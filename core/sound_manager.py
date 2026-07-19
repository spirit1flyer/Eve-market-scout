"""Sound management for EVE Market Scout.

Handles cross-platform alert sounds with support for custom sound files.
"""

import os
import sys
import shutil
import subprocess
from pathlib import Path
from tkinter import filedialog, messagebox


# Sound file name (users drop this in config dir or app dir)
ALERT_SOUND_NAME = "alert.wav"

# Supported formats per platform
WINDOWS_FORMATS = (".wav",)
LINUX_FORMATS = (".wav", ".oga", ".ogg")
MACOS_FORMATS = (".wav", ".aiff", ".mp3")


def get_config_dir() -> Path:
    """Get the user config directory for sound files.
    
    Returns:
        Path to config directory (created if needed)
    """
    if sys.platform == "win32":
        base = os.environ.get("APPDATA", os.path.expanduser("~"))
        config_dir = Path(base) / "EVEMarketScout"
    elif sys.platform == "darwin":
        config_dir = Path.home() / "Library" / "Application Support" / "EVEMarketScout"
    else:
        # Linux / other Unix
        xdg_config = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
        config_dir = Path(xdg_config) / "eve-market-scout"
    
    return config_dir


def get_sound_config_dir() -> Path:
    """Get the sound config directory, creating if needed."""
    config_dir = get_config_dir()
    config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir


def get_app_dir() -> Path:
    """Get the application directory (where main.py lives)."""
    # this file lives in core/, one level below the app root
    return Path(__file__).parent.parent


def find_custom_sound() -> str | None:
    """Find custom alert sound file.
    
    Search order:
    1. User config dir (~/.config/eve-market-scout/alert.wav)
    2. App directory (next to script, for dev/portable mode)
    
    Returns:
        Path to sound file, or None if not found
    """
    # Check config dir first
    config_sound = get_sound_config_dir() / ALERT_SOUND_NAME
    if config_sound.exists():
        return str(config_sound)
    
    # Check app directory
    app_sound = get_app_dir() / ALERT_SOUND_NAME
    if app_sound.exists():
        return str(app_sound)
    
    # Legacy: check for old SELL.WAV
    legacy_sound = get_app_dir() / "SELL.WAV"
    if legacy_sound.exists():
        return str(legacy_sound)
    
    return None


def get_system_default_sound() -> str | None:
    """Get system default sound path.
    
    Returns:
        Path to system sound, or None to use beep fallback
    """
    if sys.platform == "linux":
        # Try freedesktop sound theme locations
        candidates = [
            "/usr/share/sounds/freedesktop/stereo/complete.oga",
            "/usr/share/sounds/freedesktop/stereo/bell.oga",
            "/usr/share/sounds/freedesktop/stereo/message.oga",
        ]
        for path in candidates:
            if os.path.exists(path):
                return path
    
    # Windows and macOS use system beeps as fallback
    return None


def play_alert():
    """Play alert sound using best available method."""
    # Try custom sound first
    sound_path = find_custom_sound()
    
    # Fall back to system default
    if not sound_path:
        sound_path = get_system_default_sound()
    
    try:
        if sys.platform == "win32":
            _play_windows(sound_path)
        elif sys.platform == "linux":
            _play_linux(sound_path)
        elif sys.platform == "darwin":
            _play_macos(sound_path)
        else:
            # Unknown platform - try terminal bell
            print('\a')
    except Exception as e:
        print(f"Sound error: {e}")
        print('\a')


def _play_windows(sound_path: str | None):
    """Play sound on Windows."""
    import winsound
    if sound_path and os.path.exists(sound_path):
        winsound.PlaySound(sound_path, winsound.SND_FILENAME | winsound.SND_ASYNC)
    else:
        winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)


def _play_linux(sound_path: str | None):
    """Play sound on Linux."""
    if sound_path and os.path.exists(sound_path):
        # Use paplay for PulseAudio (handles more formats)
        # Fall back to aplay for ALSA
        if sound_path.endswith(('.oga', '.ogg')):
            # OGG/OGA needs paplay
            try:
                subprocess.Popen(
                    ["paplay", sound_path],
                    stderr=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL
                )
                return
            except FileNotFoundError:
                pass
        
        # Try aplay for WAV
        try:
            subprocess.Popen(
                ["aplay", "-q", sound_path],
                stderr=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL
            )
            return
        except FileNotFoundError:
            pass
        
        # Try paplay as last resort
        try:
            subprocess.Popen(
                ["paplay", sound_path],
                stderr=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL
            )
            return
        except FileNotFoundError:
            pass
    
    # Fallback to terminal bell
    print('\a')


def _play_macos(sound_path: str | None):
    """Play sound on macOS."""
    if sound_path and os.path.exists(sound_path):
        subprocess.Popen(
            ["afplay", sound_path],
            stderr=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL
        )
    else:
        # macOS system sound
        subprocess.Popen(
            ["afplay", "/System/Library/Sounds/Glass.aiff"],
            stderr=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL
        )


def open_sound_folder():
    """Open the sound config folder in system file manager."""
    config_dir = get_sound_config_dir()
    
    try:
        if sys.platform == "win32":
            subprocess.Popen(["explorer", str(config_dir)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(config_dir)])
        else:
            # Linux - try xdg-open
            subprocess.Popen(["xdg-open", str(config_dir)])
        return True
    except Exception as e:
        print(f"Failed to open folder: {e}")
        return False


def select_sound_file(parent_window) -> bool:
    """Open file picker to select a custom sound file.
    
    Args:
        parent_window: Tkinter parent window for dialog
        
    Returns:
        True if sound was successfully set, False otherwise
    """
    # Build file type filter based on platform
    if sys.platform == "win32":
        filetypes = [("WAV files", "*.wav"), ("All files", "*.*")]
    elif sys.platform == "darwin":
        filetypes = [
            ("Audio files", "*.wav *.aiff *.mp3"),
            ("WAV files", "*.wav"),
            ("All files", "*.*")
        ]
    else:
        filetypes = [
            ("Audio files", "*.wav *.oga *.ogg"),
            ("WAV files", "*.wav"),
            ("OGG files", "*.oga *.ogg"),
            ("All files", "*.*")
        ]
    
    filepath = filedialog.askopenfilename(
        parent=parent_window,
        title="Select Alert Sound",
        filetypes=filetypes
    )
    
    if not filepath:
        return False
    
    # Copy to config dir as alert.wav (or appropriate extension)
    config_dir = get_sound_config_dir()
    dest_path = config_dir / ALERT_SOUND_NAME
    
    try:
        shutil.copy2(filepath, dest_path)
        messagebox.showinfo(
            "Sound Set",
            f"Alert sound set successfully.\n\nFile saved to:\n{dest_path}",
            parent=parent_window
        )
        return True
    except Exception as e:
        messagebox.showerror(
            "Error",
            f"Failed to set sound file:\n{e}",
            parent=parent_window
        )
        return False


def reset_to_default(parent_window) -> bool:
    """Remove custom sound and reset to system default.
    
    Args:
        parent_window: Tkinter parent window for dialog
        
    Returns:
        True if reset successful, False otherwise
    """
    config_sound = get_sound_config_dir() / ALERT_SOUND_NAME
    
    if not config_sound.exists():
        messagebox.showinfo(
            "No Custom Sound",
            "No custom sound file is set.\nAlready using system default.",
            parent=parent_window
        )
        return False
    
    try:
        config_sound.unlink()
        messagebox.showinfo(
            "Sound Reset",
            "Custom sound removed.\nNow using system default.",
            parent=parent_window
        )
        return True
    except Exception as e:
        messagebox.showerror(
            "Error",
            f"Failed to remove custom sound:\n{e}",
            parent=parent_window
        )
        return False


def get_current_sound_status() -> str:
    """Get description of current sound configuration.
    
    Returns:
        Human-readable status string
    """
    custom = find_custom_sound()
    if custom:
        # Check if it's in config dir or app dir
        config_dir = str(get_sound_config_dir())
        if custom.startswith(config_dir):
            return f"Custom: {os.path.basename(custom)}"
        else:
            return f"Local: {os.path.basename(custom)}"
    else:
        return "System default"


def get_data_dir() -> Path:
    """Get the data directory for user files (watchlist, trades, etc).
    
    Same as config dir - centralizes all user data in one place.
    Creates directory if needed.
    
    Returns:
        Path to data directory
    """
    data_dir = get_config_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


def open_data_folder():
    """Open the data folder in system file manager.
    
    This is where watchlist.json, tracked_trades.json, etc. live.
    """
    data_dir = get_data_dir()
    
    try:
        if sys.platform == "win32":
            subprocess.Popen(["explorer", str(data_dir)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(data_dir)])
        else:
            subprocess.Popen(["xdg-open", str(data_dir)])
        return True
    except Exception as e:
        print(f"Failed to open folder: {e}")
        return False
