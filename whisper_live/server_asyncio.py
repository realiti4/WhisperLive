import os
import time
import threading
import asyncio
import json
import functools
import logging
import torch
import numpy as np
from websockets.sync.server import serve
from websockets.server import serve as serve_async
from websockets.exceptions import ConnectionClosed
from whisper_live.vad import VoiceActivityDetector
from whisper_live.faster_whisper import ServeClientFasterWhisper
try:
    from whisper_live.transcriber_tensorrt import WhisperTRTLLM
except Exception:
    pass

logging.basicConfig(level=logging.INFO)


class ClientManager:
    def __init__(self, max_clients=4, max_connection_time=600):
        """
        Initializes the ClientManager with specified limits on client connections and connection durations.

        Args:
            max_clients (int, optional): The maximum number of simultaneous client connections allowed. Defaults to 4.
            max_connection_time (int, optional): The maximum duration (in seconds) a client can stay connected. Defaults
                                                 to 600 seconds (10 minutes).
        """
        self.clients = {}
        self.start_times = {}
        self.max_clients = max_clients
        self.max_connection_time = max_connection_time

    def add_client(self, websocket, client):
        """
        Adds a client and their connection start time to the tracking dictionaries.

        Args:
            websocket: The websocket associated with the client to add.
            client: The client object to be added and tracked.
        """
        self.clients[websocket] = client
        self.start_times[websocket] = time.time()

    def get_client(self, websocket):
        """
        Retrieves a client associated with the given websocket.

        Args:
            websocket: The websocket associated with the client to retrieve.

        Returns:
            The client object if found, False otherwise.
        """
        if websocket in self.clients:
            return self.clients[websocket]
        return False

    def remove_client(self, websocket):
        """
        Removes a client and their connection start time from the tracking dictionaries. Performs cleanup on the
        client if necessary.

        Args:
            websocket: The websocket associated with the client to be removed.
        """
        client = self.clients.pop(websocket, None)
        if client:
            client.cleanup()
        self.start_times.pop(websocket, None)

    def get_wait_time(self):
        """
        Calculates the estimated wait time for new clients based on the remaining connection times of current clients.

        Returns:
            The estimated wait time in minutes for new clients to connect. Returns 0 if there are available slots.
        """
        wait_time = None
        for start_time in self.start_times.values():
            current_client_time_remaining = self.max_connection_time - (time.time() - start_time)
            if wait_time is None or current_client_time_remaining < wait_time:
                wait_time = current_client_time_remaining
        return wait_time / 60 if wait_time is not None else 0

    def is_server_full(self, websocket, options):
        """
        Checks if the server is at its maximum client capacity and sends a wait message to the client if necessary.

        Args:
            websocket: The websocket of the client attempting to connect.
            options: A dictionary of options that may include the client's unique identifier.

        Returns:
            True if the server is full, False otherwise.
        """
        if len(self.clients) >= self.max_clients:
            wait_time = self.get_wait_time()
            response = {"uid": options["uid"], "status": "WAIT", "message": wait_time}
            websocket.send(json.dumps(response))
            return True
        return False

    def is_client_timeout(self, websocket):
        """
        Checks if a client has exceeded the maximum allowed connection time and disconnects them if so, issuing a warning.

        Args:
            websocket: The websocket associated with the client to check.

        Returns:
            True if the client's connection time has exceeded the maximum limit, False otherwise.
        """
        elapsed_time = time.time() - self.start_times[websocket]
        if elapsed_time >= self.max_connection_time:
            self.clients[websocket].disconnect()
            logging.warning(f"Client with uid '{self.clients[websocket].client_uid}' disconnected due to overtime.")
            return True
        return False


class TranscriptionServer:
    RATE = 16000

    def __init__(self):
        self.client_manager = ClientManager()
        self.no_voice_activity_chunks = 0
        self.use_vad = True

    def initialize_client(
        self, websocket, options, faster_whisper_custom_model_path,
        whisper_tensorrt_path, trt_multilingual, loop
    ):
        if self.backend == "tensorrt":
            try:
                client = ServeClientTensorRT(
                    websocket,
                    multilingual=trt_multilingual,
                    language=options["language"],
                    task=options["task"],
                    client_uid=options["uid"],
                    model=whisper_tensorrt_path
                )
                logging.info("Running TensorRT backend.")
            except Exception as e:
                logging.error(f"TensorRT-LLM not supported: {e}")
                self.client_uid = options["uid"]
                websocket.send(json.dumps({
                    "uid": self.client_uid,
                    "status": "WARNING",
                    "message": "TensorRT-LLM not supported on Server yet. "
                               "Reverting to available backend: 'faster_whisper'"
                }))
                self.backend = "faster_whisper"

        if self.backend == "faster_whisper":
            if faster_whisper_custom_model_path is not None and os.path.exists(faster_whisper_custom_model_path):
                logging.info(f"Using custom model {faster_whisper_custom_model_path}")
                options["model"] = faster_whisper_custom_model_path
            client = ServeClientFasterWhisper(
                websocket,
                language=options["language"],
                task=options["task"],
                client_uid=options["uid"],
                model=options["model"],
                initial_prompt=options.get("initial_prompt"),
                vad_parameters=options.get("vad_parameters"),
                use_vad=self.use_vad,
                loop=loop
            )
            logging.info("Running faster_whisper backend.")

        self.client_manager.add_client(websocket, client)

    async def get_audio_from_websocket(self, websocket):
        """
        Receives audio buffer from websocket and creates a numpy array out of it.

        Args:
            websocket: The websocket to receive audio from.

        Returns:
            A numpy array containing the audio.
        """
        frame_data = await websocket.recv()
        if frame_data == b"END_OF_AUDIO":
            return False
        return np.frombuffer(frame_data, dtype=np.float32)

    async def handle_new_connection(self, websocket, faster_whisper_custom_model_path,
                              whisper_tensorrt_path, trt_multilingual, loop):
        try:
            logging.info("New client connected")

            options = await websocket.recv()
            options = json.loads(options)

            self.use_vad = options.get('use_vad')
            if self.client_manager.is_server_full(websocket, options):
                websocket.close()
                return False  # Indicates that the connection should not continue

            if self.backend == "tensorrt":
                self.vad_detector = VoiceActivityDetector(frame_rate=self.RATE)

            self.initialize_client(websocket, options, faster_whisper_custom_model_path,
                                   whisper_tensorrt_path, trt_multilingual, loop)
            return True
        except json.JSONDecodeError:
            logging.error("Failed to decode JSON from client")
            return False
        except ConnectionClosed:
            logging.info("Connection closed by client")
            return False
        except Exception as e:
            logging.error(f"Error during new connection initialization: {str(e)}")
            return False

    async def process_audio_frames(self, websocket):
        frame_np = await self.get_audio_from_websocket(websocket)
        client = self.client_manager.get_client(websocket)
        if frame_np is False:
            if self.backend == "tensorrt":
                client.set_eos(True)
            return False

        if self.backend == "tensorrt":
            voice_active = self.voice_activity(websocket, frame_np)
            if voice_active:
                self.no_voice_activity_chunks = 0
                client.set_eos(False)
            if self.use_vad and not voice_active:
                return True

        await client.add_frames(frame_np)
        return True

    async def recv_audio(
        self,
        websocket,
        backend="faster_whisper",
        faster_whisper_custom_model_path=None,
        whisper_tensorrt_path=None,
        trt_multilingual=False,
    ):
        """
        Receive audio chunks from a client in an infinite loop.

        Continuously receives audio frames from a connected client
        over a WebSocket connection. It processes the audio frames using a
        voice activity detection (VAD) model to determine if they contain speech
        or not. If the audio frame contains speech, it is added to the client's
        audio data for ASR.
        If the maximum number of clients is reached, the method sends a
        "WAIT" status to the client, indicating that they should wait
        until a slot is available.
        If a client's connection exceeds the maximum allowed time, it will
        be disconnected, and the client's resources will be cleaned up.

        Args:
            websocket (WebSocket): The WebSocket connection for the client.
            backend (str): The backend to run the server with.
            faster_whisper_custom_model_path (str): path to custom faster whisper model.
            whisper_tensorrt_path (str): Required for tensorrt backend.
            trt_multilingual(bool): Only used for tensorrt, True if multilingual model.

        Raises:
            Exception: If there is an error during the audio frame processing.
        """
        

        try:
            while not self.client_manager.is_client_timeout(websocket):
                if not await self.process_audio_frames(websocket):
                    break
        except ConnectionClosed:
            logging.info("Connection closed by client")
        except Exception as e:
            logging.error(f"Unexpected error: {str(e)}")
        finally:
            if self.client_manager.get_client(websocket):
                self.cleanup(websocket)
                await websocket.close()
            del websocket

    async def handler(
            self,
            websocket,
            backend="faster_whisper",
            faster_whisper_custom_model_path=None,
            whisper_tensorrt_path=None,
            trt_multilingual=False,
        ):

        loop = asyncio.get_running_loop()
        self.backend = backend
        
        # New connection
        if not await self.handle_new_connection(
            websocket,
            faster_whisper_custom_model_path,
            whisper_tensorrt_path,
            trt_multilingual,
            loop
        ):
            return
        

        # Send Client info
        client = self.client_manager.get_client(websocket)

        await websocket.send(
            json.dumps(
                {
                    "uid": client.client_uid,
                    "message": client.SERVER_READY,
                    "backend": self.backend
                }
            )
        )

        # Run Speech to Text
        loop.run_in_executor(None, client.speech_to_text)

        # s2t_task = asyncio.create_task(client.speech_to_text())
        recv_task = asyncio.create_task(self.recv_audio(websocket=websocket))

        # loop.run_forever()
        
        # await asyncio.gather(recv_task)
    
    async def run(
        self,
        host,
        port=9090,
        backend="tensorrt",
        faster_whisper_custom_model_path=None,
        whisper_tensorrt_path=None,
        trt_multilingual=False,
    ):
        """
        Run the transcription server.

        Args:
            host (str): The host address to bind the server.
            port (int): The port number to bind the server.
        """

        async with serve_async(
            functools.partial(
                self.handler,
                backend=backend,
                faster_whisper_custom_model_path=faster_whisper_custom_model_path,
                whisper_tensorrt_path=whisper_tensorrt_path,
                trt_multilingual=trt_multilingual,
            ),
            host,
            port,
        ):
            await asyncio.Future()  # run forever

    def voice_activity(self, websocket, frame_np):
        """
        Evaluates the voice activity in a given audio frame and manages the state of voice activity detection.

        This method uses the configured voice activity detection (VAD) model to assess whether the given audio frame
        contains speech. If the VAD model detects no voice activity for more than three consecutive frames,
        it sets an end-of-speech (EOS) flag for the associated client. This method aims to efficiently manage
        speech detection to improve subsequent processing steps.

        Args:
            websocket: The websocket associated with the current client. Used to retrieve the client object
                    from the client manager for state management.
            frame_np (numpy.ndarray): The audio frame to be analyzed. This should be a NumPy array containing
                                    the audio data for the current frame.

        Returns:
            bool: True if voice activity is detected in the current frame, False otherwise. When returning False
                after detecting no voice activity for more than three consecutive frames, it also triggers the
                end-of-speech (EOS) flag for the client.
        """
        if not self.vad_detector(frame_np):
            self.no_voice_activity_chunks += 1
            if self.no_voice_activity_chunks > 3:
                client = self.client_manager.get_client(websocket)
                if not client.eos:
                    client.set_eos(True)
                time.sleep(0.1)    # Sleep 100m; wait some voice activity.
            return False
        return True

    def cleanup(self, websocket):
        """
        Cleans up resources associated with a given client's websocket.

        Args:
            websocket: The websocket associated with the client to be cleaned up.
        """
        if self.client_manager.get_client(websocket):
            self.client_manager.remove_client(websocket)
