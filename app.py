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

# Enable High-DPI Awareness on Windows so widgets render sharply and don't paint black
try:
    from ctypes import windll
    windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass

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
request_show_window = False

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
    try:
        img = ImageGrab.grabclipboard()
        if isinstance(img, Image.Image):
            return img
    except Exception:
        pass
    return None

def write_png_to_clipboard(pil_img):
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
# BACKGROUND CLIPBOARD LISTENER THREAD
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

        img = get_clipboard_image()
        if img is None:
            continue

        try:
            raw_bytes = img.tobytes()
            current_hash = hashlib.md5(raw_bytes).hexdigest()
        except Exception:
            continue

        if current_hash == last_processed_hash:
            continue  # Ignore our own export

        if img.mode == "RGBA":
            alpha_channel = np.array(img)[:, :, 3]
            if np.mean(alpha_channel < 10) > 0.08:
                continue

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
        global request_show_window
        request_show_window = True

    def on_toggle_clip(icon, item):
        current_settings["auto_clip_enabled"] = not current_settings["auto_clip_enabled"]
        save_settings(current_settings)
        app.sync_ui()

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
# SETTINGS GUI (Robust, High-Contrast UI)
# ==========================================
class SettingsApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Quick Ink Clipper")
        self.root.geometry("440x600")
        self.root.minsize(400, 520)
        self.root.configure(bg="#111318")

        self.center_window(440, 600)
        self.root.protocol("WM_DELETE_WINDOW", self.hide_window)

        self.build_ui()
        self.sync_ui()

        # Start continuous 50ms heartbeat poll loop
        self.poll_tray_requests()

    def center_window(self, w, h):
        try:
            sw = self.root.winfo_screenwidth()
            sh = self.root.winfo_screenheight()
            x = max(0, (sw - w) // 2)
            y = max(0, (sh - h) // 2)
            self.root.geometry(f"{w}x{h}+{x}+{y}")
        except Exception:
            pass

    def poll_tray_requests(self):
        global request_show_window
        if request_show_window:
            request_show_window = False
            self.show_window()
        self.root.after(50, self.poll_tray_requests)

    def build_ui(self):
        # Outer Container with bright, clear borders
        self.container = tk.Frame(self.root, bg="#111318", padx=16, pady=16)
        self.container.pack(fill="both", expand=True)

        # Header Title
        header_frame = tk.Frame(self.container, bg="#111318")
        header_frame.pack(fill="x", pady=(0, 12))

        tk.Label(
            header_frame, text="Quick Ink Clipper",
            font=("Segoe UI", 14, "bold"), bg="#111318", fg="#FFFFFF"
        ).pack(anchor="w")

        self.status_lbl = tk.Label(
            header_frame, text="● Active & Monitoring (Win + Shift + S)",
            font=("Segoe UI", 9, "bold"), bg="#111318", fg="#10B981"
        )
        self.status_lbl.pack(anchor="w", pady=(2, 0))

        # Card 1: Master Controls
        card1 = tk.Frame(self.container, bg="#1A1D26", padx=14, pady=12, relief="solid", bd=1, highlightbackground="#2D3345")
        card1.pack(fill="x", pady=(0, 10))

        self.auto_clip_var = tk.BooleanVar(value=current_settings["auto_clip_enabled"])
        chk_clip = tk.Checkbutton(
            card1, text="Auto-Clip on Snip (Win + Shift + S)",
            variable=self.auto_clip_var, command=self.on_setting_change,
            bg="#1A1D26", fg="#FFFFFF", selectcolor="#111318", activebackground="#1A1D26", activeforeground="#FFFFFF",
            font=("Segoe UI", 9, "bold")
        )
        chk_clip.pack(anchor="w")

        self.sound_var = tk.BooleanVar(value=current_settings["play_sound"])
        chk_sound = tk.Checkbutton(
            card1, text="Play audio chime when ready",
            variable=self.sound_var, command=self.on_setting_change,
            bg="#1A1D26", fg="#94A3B8", selectcolor="#111318", activebackground="#1A1D26", activeforeground="#FFFFFF",
            font=("Segoe UI", 8)
        )
        chk_sound.pack(anchor="w", pady=(4, 0))

        # Card 2: Ink Output Mode
        card2 = tk.Frame(self.container, bg="#1A1D26", padx=14, pady=12, relief="solid", bd=1, highlightbackground="#2D3345")
        card2.pack(fill="x", pady=(0, 10))

        tk.Label(
            card2, text="RENDER INK AS", font=("Segoe UI", 8, "bold"),
            bg="#1A1D26", fg="#818CF8"
        ).pack(anchor="w", pady=(0, 6))

        self.ink_mode_var = tk.StringVar(value=current_settings["ink_mode"])
        modes = [
            ("Pitch Black (Vector Sharp)", "black"),
            ("Preserve Tint (Keep Grey Rivers / Colors)", "preserve"),
            ("Crisp White (For Dark Notes)", "white")
        ]
        for label, val in modes:
            rb = tk.Radiobutton(
                card2, text=label, value=val, variable=self.ink_mode_var,
                command=self.on_setting_change, bg="#1A1D26", fg="#E2E8F0",
                selectcolor="#111318", activebackground="#1A1D26", activeforeground="#FFFFFF",
                font=("Segoe UI", 8)
            )
            rb.pack(anchor="w", pady=2)

        # Card 3: Precision Tuning Sliders
        card3 = tk.Frame(self.container, bg="#1A1D26", padx=14, pady=12, relief="solid", bd=1, highlightbackground="#2D3345")
        card3.pack(fill="x", pady=(0, 10))

        # Slider: Tolerance
        f_tol = tk.Frame(card3, bg="#1A1D26")
        f_tol.pack(fill="x")
        tk.Label(f_tol, text="White Background Cutoff", font=("Segoe UI", 8), bg="#1A1D26", fg="#CBD5E1").pack(side="left")
        self.tol_val_lbl = tk.Label(f_tol, text=f"{current_settings['white_tolerance']}%", font=("Segoe UI", 8, "bold"), bg="#1A1D26", fg="#38BDF8")
        self.tol_val_lbl.pack(side="right")

        self.tol_scale = tk.Scale(
            card3, from_=10, to=80, orient="horizontal", bg="#1A1D26", fg="#FFFFFF",
            highlightthickness=0, troughcolor="#2D3345", showvalue=0, command=self.on_slider_change
        )
        self.tol_scale.set(current_settings["white_tolerance"])
        self.tol_scale.pack(fill="x", pady=(2, 6))

        # Slider: Softness
        f_soft = tk.Frame(card3, bg="#1A1D26")
        f_soft.pack(fill="x")
        tk.Label(f_soft, text="Edge Softness (Defringe)", font=("Segoe UI", 8), bg="#1A1D26", fg="#CBD5E1").pack(side="left")
        self.soft_val_lbl = tk.Label(f_soft, text=f"{current_settings['softness']}px", font=("Segoe UI", 8, "bold"), bg="#1A1D26", fg="#38BDF8")
        self.soft_val_lbl.pack(side="right")

        self.soft_scale = tk.Scale(
            card3, from_=0, to=12, orient="horizontal", bg="#1A1D26", fg="#FFFFFF",
            highlightthickness=0, troughcolor="#2D3345", showvalue=0, command=self.on_slider_change
        )
        self.soft_scale.set(current_settings["softness"])
        self.soft_scale.pack(fill="x", pady=(2, 6))

        # Checkbox: Auto Crop
        self.crop_var = tk.BooleanVar(value=current_settings["auto_crop"])
        chk_crop = tk.Checkbutton(
            card3, text="Auto-Trim Empty Margins (Tight Sticker)",
            variable=self.crop_var, command=self.on_setting_change,
            bg="#1A1D26", fg="#E2E8F0", selectcolor="#111318", activebackground="#1A1D26", activeforeground="#FFFFFF",
            font=("Segoe UI", 8)
        )
        chk_crop.pack(anchor="w", pady=(4, 0))

        # Hide to Tray Action Button
        btn_hide = tk.Button(
            self.container, text="✓ Save & Minimize to Taskbar Tray", command=self.hide_window,
            bg="#2563EB", fg="#FFFFFF", activebackground="#1D4ED8", activeforeground="#FFFFFF",
            relief="flat", font=("Segoe UI", 9, "bold"), pady=8, cursor="hand2"
        )
        btn_hide.pack(fill="x", pady=(6, 0))

    def on_setting_change(self):
        current_settings["auto_clip_enabled"] = self.auto_clip_var.get()
        current_settings["play_sound"] = self.sound_var.get()
        current_settings["ink_mode"] = self.ink_mode_var.get()
        current_settings["auto_crop"] = self.crop_var.get()
        save_settings(current_settings)
        self.sync_ui()

    def on_slider_change(self, _=None):
        if not hasattr(self, 'tol_scale') or not hasattr(self, 'soft_scale'):
            return
        current_settings["white_tolerance"] = self.tol_scale.get()
        current_settings["softness"] = self.soft_scale.get()
        if hasattr(self, 'tol_val_lbl'):
            self.tol_val_lbl.config(text=f"{self.tol_scale.get()}%")
        if hasattr(self, 'soft_val_lbl'):
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
        self.root.state('normal')
        self.root.lift()
        self.root.focus_force()
        # Force Windows to repaint the window canvas and children immediately
        self.root.update_idletasks()
        self.root.update()

# ==========================================
# MAIN ENTRY POINT
# ==========================================
if __name__ == "__main__":
    listener_thread = threading.Thread(target=clipboard_listener, daemon=True)
    listener_thread.start()

    root = tk.Tk()
    app = SettingsApp(root)
    setup_tray(app)

    # Force initial display
    app.show_window()

    root.mainloop()
