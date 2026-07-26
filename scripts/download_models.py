"""
Meeting Scribe — Model Downloader
Pre-downloads all required AI models so the app works fully offline.
Run this ONCE after installation.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.utils.file_utils import get_models_dir
from src.utils.hardware_probe import recommend_whisper_model


def download_whisper_model(model_size: str = None):
    """Download a faster-whisper model to local cache."""
    from faster_whisper import WhisperModel

    if model_size is None:
        model_size = recommend_whisper_model()

    models_dir = str(get_models_dir())
    print(f"Download directory: {models_dir}")
    print(f"Downloading Whisper model: {model_size}")
    print("This may take a few minutes on first run...\n")

    # This triggers the download into models_dir
    model = WhisperModel(
        model_size,
        device="cpu",
        compute_type="int8",
        download_root=models_dir,
    )

    print(f"\n[OK] Model '{model_size}' downloaded and cached successfully!")
    print(f"Location: {models_dir}")
    print("The app will now work fully offline.")
    return model_size


def download_silero_vad():
    """Download silero-vad model (used by faster-whisper internally)."""
    print("\nDownloading silero-vad model...")
    try:
        import torch
        model, utils = torch.hub.load(
            repo_or_dir='snakers4/silero-vad',
            model='silero_vad',
            force_reload=False,
            onnx=False,
            trust_repo=True
        )
        print("[OK] silero-vad downloaded and cached!")
    except ImportError:
        print("[SKIP] PyTorch not installed - silero-vad standalone not needed")
        print("       (faster-whisper has its own built-in VAD)")


if __name__ == "__main__":
    print("=" * 60)
    print("  Meeting Scribe - Model Setup")
    print("=" * 60)
    print()

    # Parse optional model size argument
    model_size = None
    if len(sys.argv) > 1:
        model_size = sys.argv[1]
        print(f"Using specified model: {model_size}")
    else:
        model_size = recommend_whisper_model()
        print(f"Auto-detected recommended model: {model_size}")

    print()
    downloaded = download_whisper_model(model_size)

    # Also try to download a small model as fallback
    if downloaded != "tiny":
        print("\nAlso downloading 'tiny' model as fast fallback...")
        try:
            download_whisper_model("tiny")
        except Exception as e:
            print(f"[WARN] Could not download tiny model: {e}")

    print("\n" + "=" * 60)
    print("  Setup complete! You can now run: python main.py")
    print("=" * 60)
