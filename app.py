import io
import os
import json
import time
import hashlib
import threading
import winsound
import numpy as np
from PIL import Image, ImageGrab, ImageDraw
import win32clipboard
import win32con
import tkinter as tk
from tkinter import ttk, colorchooser
import pystray

# ==========================================
# CONFIGURATION & SETTINGS STORAGE
# ==========================================
CONFIG_DIR = os.path.join(os.environ.get("APPDATA", "."), "QuickInkClipper")
CONFIG_FILE = os.path.join(CONFIG_DIR, "settings.json")

DEFAULT_SETTINGS = {
    "auto_clip_enabled": True,
    "play_sound": True,
    "ink_mode": "black",  # "black", "preserve", "white", "custom"
    "custom_color": "#3B82F6",
    "white_tolerance": 32,
    "softness": 3,
    "ink_boost": 1.4,
    "auto_crop": True
}

def load_settings():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                data = json.load(f)
                return {**DEFAULT_SETTINGS, **data}
        except Exception:
            pass
    return DEFAULT_SETTINGS.copy()

def save_settings(settings):
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(CONFIG_FILE, "w") as f:
            json.dump(settings, f, indent=2)
    except Exception as e:
        print("Failed to save settings:", e)

current_settings = load_settings()

# ==========================================
# MATHEMATICAL MATTING ENGINE
# ==========================================
def hex_to_rgb(hex_str):
    hex_str = hex_str.lstrip('#')
    return tuple(int(hex_str[i:i+2], 16) for i in (0, 2, 4))

def process_image(pil_img, cfg):
    """Executes subpixel Color-to-Alpha vector extraction."""
    img = pil_img.convert("RGBA")
    arr = np.array(img, dtype=np.float32)

    r = arr[:, :, 0]
    g = arr[:, :, 1]
    b = arr[:, :, 2]

    # Standard Rec. 709 Luminance
    lum = 0.2126 * r + 0.7152 * g + 0.0722 * b
    dist_from_white = 255.0 - lum

    tol = (float(cfg["white_tolerance"]) / 100.0) * 255.0
    soft = float(cfg["softness"]) * 3.0
    boost = float(cfg["ink_boost"])
    mode = cfg["ink_mode"]

    # Compute Alpha Channel
    alpha = np.zeros_like(lum)
    if soft > 0:
        ramp = (dist_from_white - (tol - soft)) / (soft * 2.0)
        alpha = np.clip(ramp * 255.0, 0.0, 255.0)
    else:
        alpha = np.where(dist_from_white >= tol, 255.0, 0.0)

    out = np.zeros_like(arr, dtype=np.uint8)
    out[:, :, 3] = alpha.astype(np.uint8)

    # Color Mapping
    if mode == "black":
        out[:, :, 0] = 0
        out[:, :, 1] = 0
        out[:, :, 2] = 0
    elif mode == "white":
        out[:, :, 0] = 255
        out[:, :, 1] = 255
        out[:, :, 2] = 255
    elif mode == "custom":
        cr, cg, cb = hex_to_rgb(cfg.get("custom_color", "#3B82F6"))
        out[:, :, 0] = cr
        out[:, :, 1] = cg
        out[:, :, 2] = cb
    else:
        # Preserve original line/river colors with defringing
        out[:, :, 0] = np.clip(r / boost, 0, 255).astype(np.uint8)
        out[:, :, 1] = np.clip(g / boost, 0, 255).astype(np.uint8)
        out[:, :, 2] = np.clip(b / boost, 0, 255).astype(np.uint8)

    result = Image.fromarray(out, mode="RGBA")

    # Auto Crop Transparent Margins
    if cfg.get("auto_crop", True):
        bbox = result.getbbox()
        if bbox:
            pad = 6
            w, h = result.size
            crop_box = (
                max(0, bbox[0] - pad),
                max(0, bbox[1] - pad),
                min(w, bbox[2] + pad),
                min(h, bbox[3] + pad)
            )
            result = result.crop(crop_box)

    return result

# ==========================================
# WINDOWS CLIPBOARD I/O & LOOP GUARDS
# ==========================================
last_processed_hash = None

def get_clipboard_image():
    """Safely retrieves an image from clipboard."""
    try:
        img = ImageGrab.grabclipboard()
        if isinstance(img, Image.Image):
            return img
    except Exception:
        pass
    return None

def write_png_to_clipboard(pil_img):
    """Pushes a raw transparent PNG directly into Windows Clipboard."""
    global last_processed_hash
    buf = io.BytesIO()
    pil_img.save(buf, format="PNG")
    png_bytes = buf.getvalue()

    # Track hash to prevent infinite loop
    last_processed_hash = hashlib.md5(png_bytes).hexdigest()

    for _ in range(5):
        try:
            win32clipboard.OpenClipboard()
            win32clipboard.EmptyClipboard()
            png_format = win32clipboard.RegisterClipboardFormat("PNG")
            win32clipboard.SetClipboardData(png_format, png_bytes)
            win32clipboard.CloseClipboard()
            return True
        except Exception:
            time.sleep(0.05)
    return False

# ==========================================
# BACKGROUND LISTENER THREAD
# ==========================================
def clipboard_listener():
    global last_processed_hash
    last_seq = None

    while True:
        time.sleep(0.15)
        if not current_settings.get("auto_clip_enabled", True):
            continue

        try:
            seq = win32clipboard.GetClipboardSequenceNumber()
            if seq == last_seq:
                continue
            last_seq = seq
        except Exception:
            continue

        # Check if new content is an image
        img = get_clipboard_image()
        if img is None:
            continue

        # Fingerprint current clipboard content
        try:
            raw_bytes = img.tobytes()
            current_hash = hashlib.md5(raw_bytes).hexdigest()
        except Exception:
            continue

        if current_hash == last_processed_hash:
            continue  # Ignore our own export

        # Safety Check: If image is already largely transparent, don't re-process
        if img.mode == "RGBA":
            alpha_channel = np.array(img)[:, :, 3]
            if np.mean(alpha_channel < 10) > 0.08:
                continue

        # Execute processing
        try:
            clean_png = process_image(img, current_settings)
            success = write_png_to_clipboard(clean_png)
            if success and current_settings.get("play_sound", True):
                winsound.MessageBeep(winsound.MB_ICONASTERISK)
        except Exception as e:
            print("Processing error:", e)

# ==========================================
# SYSTEM TRAY INTEGRATION
# ==========================================
def create_tray_icon():
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([4, 4, 60, 60], radius=14, fill="#3B82F6")
    d.text((18, 12), "✂", fill="white", font_size=32)
    return img

def setup_tray(app):
    icon_image = create_tray_icon()

    def on_open(icon, item):
        app.root.after(0, app.show_window)

    def on_toggle_clip(icon, item):
        current_settings["auto_clip_enabled"] = not current_settings["auto_clip_enabled"]
        save_settings(current_settings)
        app.root.after(0, app.sync_ui)

    def on_exit(icon, item):
        icon.stop()
        app.root.after(0, app.root.destroy)

    menu = pystray.Menu(
        pystray.MenuItem("Settings", on_open, default=True),
        pystray.MenuItem("Enable Auto-Clip", on_toggle_clip, checked=lambda item: current_settings["auto_clip_enabled"]),
        pystray.MenuItem("Exit", on_exit)
    )

    tray = pystray.Icon("QuickInkClipper", icon_image, "Quick Ink Clipper", menu)
    threading.Thread(target=tray.run, daemon=True).start()
    return tray

# ==========================================
# SETTINGS GUI (Apple / Minimalist Style)
# ==========================================
class SettingsApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Quick Ink Clipper")
        self.root.geometry("420x540")
        self.root.resizable(False, False)
        self.root.configure(bg="#0E1017")

        # Intercept Close Button -> Hide to Tray
        self.root.protocol("WM_DELETE_WINDOW", self.hide_window)

        self.build_ui()
        self.sync_ui()

    def build_ui(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TLabel", background="#0E1017", foreground="#F8FAFC", font=("Segoe UI", 9))
        style.configure("Header.TLabel", font=("Segoe UI", 12, "bold"), foreground="#FFFFFF")
        style.configure("Status.TLabel", font=("Segoe UI", 9, "bold"))
        style.configure("Card.TFrame", background="#161822")

        container = tk.Frame(self.root, bg="#0E1017", padx=20, pady=20)
        container.pack(fill="both", expand=True)

        # Header Banner
        header_frame = tk.Frame(container, bg="#0E1017")
        header_frame.pack(fill="x", pady=(0, 16))

        tk.Label(header_frame, text="Quick Ink Clipper", font=("Segoe UI", 13, "bold"), bg="#0E1017", fg="#FFFFFF").pack(anchor="w")
        self.status_lbl = tk.Label(header_frame, text="● Active & Monitoring", font=("Segoe UI", 8, "bold"), bg="#0E1017", fg="#10B981")
        self.status_lbl.pack(anchor="w", pady=(2, 0))

        # Main Toggle Card
        toggle_card = tk.Frame(container, bg="#161822", padx=14, pady=12, highlightthickness=1, highlightbackground="#242838")
        toggle_card.pack(fill="x", pady=(0, 12))

        self.auto_clip_var = tk.BooleanVar(value=current_settings["auto_clip_enabled"])
        chk_clip = tk.Checkbutton(
            toggle_card, text="Auto-Clip on Snip (Win + Shift + S)",
            variable=self.auto_clip_var, command=self.on_setting_change,
            bg="#161822", fg="#FFFFFF", selectcolor="#0E1017", activebackground="#161822", activeforeground="#FFFFFF",
            font=("Segoe UI", 9, "bold")
        )
        chk_clip.pack(anchor="w")

        self.sound_var = tk.BooleanVar(value=current_settings["play_sound"])
        chk_sound = tk.Checkbutton(
            toggle_card, text="Play audio chime when ready",
            variable=self.sound_var, command=self.on_setting_change,
            bg="#161822", fg="#94A3B8", selectcolor="#0E1017", activebackground="#161822", activeforeground="#FFFFFF",
            font=("Segoe UI", 8)
        )
        chk_sound.pack(anchor="w", pady=(4, 0))

        # Ink Output Mode
        mode_card = tk.Frame(container, bg="#161822", padx=14, pady=12, highlightthickness=1, highlightbackground="#242838")
        mode_card.pack(fill="x", pady=(0, 12))

        tk.Label(mode_card, text="RENDER INK AS", font=("Segoe UI", 8, "bold"), bg="#161822", fg="#94A3B8").pack(anchor="w", pady=(0, 8))

        self.ink_mode_var = tk.StringVar(value=current_settings["ink_mode"])
        modes = [
            ("Pitch Black (Vector Sharp)", "black"),
            ("Preserve Tint (Keep Grey Rivers / Colors)", "preserve"),
            ("Crisp White (For Dark Notes)", "white"),
            ("Custom Color", "custom")
        ]
        for label, val in modes:
            rb = tk.Radiobutton(
                mode_card, text=label, value=val, variable=self.ink_mode_var,
                command=self.on_setting_change, bg="#161822", fg="#E2E8F0",
                selectcolor="#0E1017", activebackground="#161822", activeforeground="#FFFFFF",
                font=("Segoe UI", 8)
            )
            rb.pack(anchor="w", pady=2)

        # Fine Tuning Sliders
        tune_card = tk.Frame(container, bg="#161822", padx=14, pady=12, highlightthickness=1, highlightbackground="#242838")
        tune_card.pack(fill="x", pady=(0, 12))

        # Slider 1: Tolerance
        lbl_f1 = tk.Frame(tune_card, bg="#161822")
        lbl_f1.pack(fill="x")
        tk.Label(lbl_f1, text="White Background Cutoff", font=("Segoe UI", 8), bg="#161822", fg="#94A3B8").pack(side="left")
        self.tol_val_lbl = tk.Label(lbl_f1, text=f"{current_settings['white_tolerance']}%", font=("Segoe UI", 8), bg="#161822", fg="#FFFFFF")
        self.tol_val_lbl.pack(side="right")

        self.tol_scale = tk.Scale(
            tune_card, from_=10, to=80, orient="horizontal", bg="#161822", fg="#FFFFFF",
            highlightthickness=0, troughcolor="#242838", showvalue=0, command=self.on_slider_change
        )
        self.tol_scale.set(current_settings["white_tolerance"])
        self.tol_scale.pack(fill="x", pady=(2, 8))

        # Slider 2: Softness
        lbl_f2 = tk.Frame(tune_card, bg="#161822")
        lbl_f2.pack(fill="x")
        tk.Label(lbl_f2, text="Edge Softness (Defringe)", font=("Segoe UI", 8), bg="#161822", fg="#94A3B8").pack(side="left")
        self.soft_val_lbl = tk.Label(lbl_f2, text=f"{current_settings['softness']}px", font=("Segoe UI", 8), bg="#161822", fg="#FFFFFF")
        self.soft_val_lbl.pack(side="right")

        self.soft_scale = tk.Scale(
            tune_card, from_=0, to=12, orient="horizontal", bg="#161822", fg="#FFFFFF",
            highlightthickness=0, troughcolor="#242838", showvalue=0, command=self.on_slider_change
        )
        self.soft_scale.set(current_settings["softness"])
        self.soft_scale.pack(fill="x", pady=(2, 6))

        # Auto Crop Checkbox
        self.crop_var = tk.BooleanVar(value=current_settings["auto_crop"])
        chk_crop = tk.Checkbutton(
            tune_card, text="Auto-Trim Empty Margins (Tight Sticker)",
            variable=self.crop_var, command=self.on_setting_change,
            bg="#161822", fg="#E2E8F0", selectcolor="#0E1017", activebackground="#161822", activeforeground="#FFFFFF",
            font=("Segoe UI", 8)
        )
        chk_crop.pack(anchor="w", pady=(4, 0))

        # Minimize Hint
        tk.Label(
            container, text="Closing this window minimizes it to the Taskbar Tray.",
            font=("Segoe UI", 8), bg="#0E1017", fg="#64748B"
        ).pack(side="bottom")

    def on_setting_change(self):
        current_settings["auto_clip_enabled"] = self.auto_clip_var.get()
        current_settings["play_sound"] = self.sound_var.get()
        current_settings["ink_mode"] = self.ink_mode_var.get()
        current_settings["auto_crop"] = self.crop_var.get()
        save_settings(current_settings)
        self.sync_ui()

    def on_slider_change(self, _):
        current_settings["white_tolerance"] = self.tol_scale.get()
        current_settings["softness"] = self.soft_scale.get()
        self.tol_val_lbl.config(text=f"{self.tol_scale.get()}%")
        self.soft_val_lbl.config(text=f"{self.soft_scale.get()}px")
        save_settings(current_settings)

    def sync_ui(self):
        self.auto_clip_var.set(current_settings["auto_clip_enabled"])
        if current_settings["auto_clip_enabled"]:
            self.status_lbl.config(text="● Active & Monitoring (Win + Shift + S)", fg="#10B981")
        else:
            self.status_lbl.config(text="○ Paused", fg="#EF4444")

    def hide_window(self):
        self.root.withdraw()

    def show_window(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

# ==========================================
# MAIN ENTRY POINT
# ==========================================
if __name__ == "__main__":
    # Start Clipboard Listener in background
    listener_thread = threading.Thread(target=clipboard_listener, daemon=True)
    listener_thread.start()

    # Launch GUI
    root = tk.Tk()
    app = SettingsApp(root)
    setup_tray(app)
    root.mainloop()
