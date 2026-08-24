import sys
import time

import torch
from transformers import pipeline

MODEL_ID = "MediaTek-Research/Breeze-ASR-25"


def get_device():
    if torch.backends.mps.is_available():
        return "mps"
    if torch.cuda.is_available():
        return "cuda"
    return "cpu"


def main():
    audio_path = sys.argv[1] if len(sys.argv) > 1 else "samples/test_zh_tw.wav"
    device = get_device()
    print(f"Loading {MODEL_ID} on device={device} ...")

    t0 = time.time()
    asr = pipeline(
        "automatic-speech-recognition",
        model=MODEL_ID,
        device=device,
        torch_dtype=torch.float16 if device != "cpu" else torch.float32,
    )
    print(f"Model loaded in {time.time() - t0:.1f}s")

    t0 = time.time()
    result = asr(audio_path, return_timestamps=True)
    print(f"Inference done in {time.time() - t0:.1f}s\n")

    print("Transcript:")
    print(result["text"])

    if "chunks" in result:
        print("\nTimestamps:")
        for chunk in result["chunks"]:
            print(f"  {chunk['timestamp']}: {chunk['text']}")


if __name__ == "__main__":
    main()
