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

# Windows High-DPI Awareness (Fixes blurry fonts & black-screen paint bugs)
try:
    from ctypes import windll
    windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass

# ==========================================
# CONFIGURATION & PERSISTENT STORAGE
# ==========================================
CONFIG_DIR = os.path.join(os.environ.get("APPDATA", "."), "QuickInkClipper")
CONFIG_FILE = os.path.join(CONFIG_DIR, "settings.json")

DEFAULT_SETTINGS = {
    "auto_clip_enabled": True,
    "play_sound": True,
    "bg_detection_mode": "auto",  # "auto", "white", "dark"
    "ink_mode": "black",          # "black", "preserve", "white", "custom"
    "custom_color": "#3B82F6",
    "sensitivity": 28,            # Background noise threshold
    "softness": 4,                # Antialiasing transition range
    "ink_boost": 1.3,             # Ink density boost
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
# MATHEMATICAL MATTING ENGINE (V2)
# ==========================================
def hex_to_rgb(hex_str):
    hex_str = hex_str.lstrip('#')
    return tuple(int(hex_str[i:i+2], 16) for i in (0, 2, 4))

def estimate_background_color(arr_rgb, mode="auto"):
    """
    Samples perimeter edges (4 borders) using median filtering
    to extract the true background color B, unaffected by text.
    """
    if mode == "white":
        return np.array([255.0, 255.0, 255.0], dtype=np.float32)
    elif mode == "dark":
        return np.array([0.0, 0.0, 0.0], dtype=np.float32)

    h, w = arr_rgb.shape[:2]
    pad_y = max(1, int(h * 0.035))
    pad_x = max(1, int(w * 0.035))

    top = arr_rgb[:pad_y, :, :3].reshape(-1, 3)
    bottom = arr_rgb[-pad_y:, :, :3].reshape(-1, 3)
    left = arr_rgb[:, :pad_x, :3].reshape(-1, 3)
    right = arr_rgb[:, -pad_x:, :3].reshape(-1, 3)

    perimeter = np.vstack([top, bottom, left, right])
    bg_color = np.median(perimeter, axis=0)
    return bg_color

def process_image(pil_img, cfg):
    """
    V2 Continuous Color-to-Alpha Matting Engine
    - Multi-background support
    - Soft continuous antialiased alpha
    - Foreground color decontamination (no white halos)
    """
    img = pil_img.convert("RGBA")
    arr = np.array(img, dtype=np.float32)

    r = arr[:, :, 0]
    g = arr[:, :, 1]
    b = arr[:, :, 2]

    # 1. Estimate Background Color
    bg = estimate_background_color(arr, cfg.get("bg_detection_mode", "auto"))
    bg_r, bg_g, bg_b = bg[0], bg[1], bg[2]

    # 2. Euclidean Color Distance from Background
    # Works universally on white, off-white, sepia, chalkboard, and slides
    diff_r = r - bg_r
    diff_g = g - bg_g
    diff_b = b - bg_b
    dist = np.sqrt(diff_r * diff_r + diff_g * diff_g + diff_b * diff_b)

    # 3. Continuous Soft Alpha Calculation
    sens = float(cfg.get("sensitivity", 28))
    soft = float(cfg.get("softness", 4))
    boost = float(cfg.get("ink_boost", 1.3))
    mode = cfg.get("ink_mode", "black")

    # Dynamic Noise Threshold & Transition Range
    t_noise = sens * 1.35
    t_span = soft * 14.0 + 35.0
    t_solid = t_noise + t_span

    # Continuous alpha ramp (0.0 to 1.0)
    alpha_norm = np.clip((dist - t_noise) / (t_solid - t_noise), 0.0, 1.0)

    # Gamma curve (0.85) to preserve delicate Hindi matras, accents, and thin strokes
    alpha_curved = np.power(alpha_norm, 0.85)
    alpha_255 = alpha_curved * 255.0

    out = np.zeros_like(arr, dtype=np.uint8)
    out[:, :, 3] = np.clip(alpha_255, 0, 255).astype(np.uint8)

    # 4. Color Assignment & Decontamination
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
        # Preserve Tint Mode: Foreground Decontamination
        # Formula: F = (C - (1 - alpha) * B) / alpha
        a_safe = np.clip(alpha_curved, 0.001, 1.0)
        inv_a = 1.0 - a_safe

        # Decontaminate background color bleeding from edge pixels
        decontam_r = np.clip((r - inv_a * bg_r) / a_safe, 0.0, 255.0)
        decontam_g = np.clip((g - inv_a * bg_g) / a_safe, 0.0, 255.0)
        decontam_b = np.clip((b - inv_a * bg_b) / a_safe, 0.0, 255.0)

        # Apply ink boost
        out[:, :, 0] = np.clip(decontam_r / boost, 0, 255).astype(np.uint8)
        out[:, :, 1] = np.clip(decontam_g / boost, 0, 255).astype(np.uint8)
        out[:, :, 2] = np.clip(decontam_b / boost, 0, 255).astype(np.uint8)

    result = Image.fromarray(out, mode="RGBA")

    # 5. Low-Alpha-Aware Crop (Leaves padding so thin strokes are never clipped)
    if cfg.get("auto_crop", True):
        alpha_mask = out[:, :, 3] > 8
        coords = np.argwhere(alpha_mask)
        if len(coords) > 0:
            y_min, x_min = coords.min(axis=0)
            y_max, x_max = coords.max(axis=0)
            pad = 6
            w, h = result.size
            crop_box = (
                max(0, x_min - pad),
                max(0, y_min - pad),
                min(w, x_max + pad),
                min(h, y_max + pad)
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

    # Track hash to prevent loop
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
            continue

        # If already predominantly transparent, ignore
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
        self.root.title("Quick Ink Clipper Pro")
        self.root.geometry("450x640")
        self.root.minsize(420, 560)
        self.root.configure(bg="#111318")

        self.center_window(450, 640)
        self.root.protocol("WM_DELETE_WINDOW", self.hide_window)

        self.build_ui()
        self.sync_ui()

        # 50ms heartbeat poll to catch tray clicks
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
        self.container = tk.Frame(self.root, bg="#111318", padx=16, pady=16)
        self.container.pack(fill="both", expand=True)

        # Header Title
        header_frame = tk.Frame(self.container, bg="#111318")
        header_frame.pack(fill="x", pady=(0, 10))

        tk.Label(
            header_frame, text="Quick Ink Clipper Pro",
            font=("Segoe UI", 13, "bold"), bg="#111318", fg="#FFFFFF"
        ).pack(anchor="w")

        self.status_lbl = tk.Label(
            header_frame, text="● Active & Monitoring (Win + Shift + S)",
            font=("Segoe UI", 8, "bold"), bg="#111318", fg="#10B981"
        )
        self.status_lbl.pack(anchor="w", pady=(2, 0))

        # Card 1: Master Toggle
        card1 = tk.Frame(self.container, bg="#1A1D26", padx=12, pady=10, relief="solid", bd=1, highlightbackground="#2D3345")
        card1.pack(fill="x", pady=(0, 8))

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
        chk_sound.pack(anchor="w", pady=(2, 0))

        # Card 2: Background Detection
        card2 = tk.Frame(self.container, bg="#1A1D26", padx=12, pady=10, relief="solid", bd=1, highlightbackground="#2D3345")
        card2.pack(fill="x", pady=(0, 8))

        tk.Label(
            card2, text="SOURCE BACKGROUND COLOR", font=("Segoe UI", 8, "bold"),
            bg="#1A1D26", fg="#38BDF8"
        ).pack(anchor="w", pady=(0, 4))

        self.bg_mode_var = tk.StringVar(value=current_settings.get("bg_detection_mode", "auto"))
        bg_modes = [
            ("✨ Auto-Detect (Any Color: White, Sepia, Chalkboard, Slide)", "auto"),
            ("Force Pure White Paper", "white"),
            ("Force Dark / Blackboard", "dark")
        ]
        for label, val in bg_modes:
            rb = tk.Radiobutton(
                card2, text=label, value=val, variable=self.bg_mode_var,
                command=self.on_setting_change, bg="#1A1D26", fg="#E2E8F0",
                selectcolor="#111318", activebackground="#1A1D26", activeforeground="#FFFFFF",
                font=("Segoe UI", 8)
            )
            rb.pack(anchor="w", pady=1)

        # Card 3: Ink Output Mode
        card3 = tk.Frame(self.container, bg="#1A1D26", padx=12, pady=10, relief="solid", bd=1, highlightbackground="#2D3345")
        card3.pack(fill="x", pady=(0, 8))

        tk.Label(
            card3, text="RENDER INK AS", font=("Segoe UI", 8, "bold"),
            bg="#1A1D26", fg="#818CF8"
        ).pack(anchor="w", pady=(0, 4))

        self.ink_mode_var = tk.StringVar(value=current_settings["ink_mode"])
        ink_modes = [
            ("Pitch Black (Vector Sharp)", "black"),
            ("Preserve Tint & Rivers (Halo-Free Decontaminated)", "preserve"),
            ("Crisp White (For Dark Notes)", "white")
        ]
        for label, val in ink_modes:
            rb = tk.Radiobutton(
                card3, text=label, value=val, variable=self.ink_mode_var,
                command=self.on_setting_change, bg="#1A1D26", fg="#E2E8F0",
                selectcolor="#111318", activebackground="#1A1D26", activeforeground="#FFFFFF",
                font=("Segoe UI", 8)
            )
            rb.pack(anchor="w", pady=1)

        # Card 4: Precision Tuning Sliders
        card4 = tk.Frame(self.container, bg="#1A1D26", padx=12, pady=10, relief="solid", bd=1, highlightbackground="#2D3345")
        card4.pack(fill="x", pady=(0, 8))

        # Sensitivity / Noise Floor
        f_sens = tk.Frame(card4, bg="#1A1D26")
        f_sens.pack(fill="x")
        tk.Label(f_sens, text="Background Noise Cutoff", font=("Segoe UI", 8), bg="#1A1D26", fg="#CBD5E1").pack(side="left")
        self.sens_val_lbl = tk.Label(f_sens, text=f"{current_settings['sensitivity']}", font=("Segoe UI", 8, "bold"), bg="#1A1D26", fg="#38BDF8")
        self.sens_val_lbl.pack(side="right")

        self.sens_scale = tk.Scale(
            card4, from_=10, to=60, orient="horizontal", bg="#1A1D26", fg="#FFFFFF",
            highlightthickness=0, troughcolor="#2D3345", showvalue=0, command=self.on_slider_change
        )
        self.sens_scale.set(current_settings["sensitivity"])
        self.sens_scale.pack(fill="x", pady=(1, 6))

        # Softness
        f_soft = tk.Frame(card4, bg="#1A1D26")
        f_soft.pack(fill="x")
        tk.Label(f_soft, text="Edge Softness (Antialiasing)", font=("Segoe UI", 8), bg="#1A1D26", fg="#CBD5E1").pack(side="left")
        self.soft_val_lbl = tk.Label(f_soft, text=f"{current_settings['softness']}px", font=("Segoe UI", 8, "bold"), bg="#1A1D26", fg="#38BDF8")
        self.soft_val_lbl.pack(side="right")

        self.soft_scale = tk.Scale(
            card4, from_=1, to=10, orient="horizontal", bg="#1A1D26", fg="#FFFFFF",
            highlightthickness=0, troughcolor="#2D3345", showvalue=0, command=self.on_slider_change
        )
        self.soft_scale.set(current_settings["softness"])
        self.soft_scale.pack(fill="x", pady=(1, 4))

        # Auto Crop
        self.crop_var = tk.BooleanVar(value=current_settings["auto_crop"])
        chk_crop = tk.Checkbutton(
            card4, text="Auto-Trim Empty Margins (Tight Sticker)",
            variable=self.crop_var, command=self.on_setting_change,
            bg="#1A1D26", fg="#E2E8F0", selectcolor="#111318", activebackground="#1A1D26", activeforeground="#FFFFFF",
            font=("Segoe UI", 8)
        )
        chk_crop.pack(anchor="w", pady=(3, 0))

        # Hide to Tray Action Button
        btn_hide = tk.Button(
            self.container, text="✓ Save & Minimize to Taskbar Tray", command=self.hide_window,
            bg="#2563EB", fg="#FFFFFF", activebackground="#1D4ED8", activeforeground="#FFFFFF",
            relief="flat", font=("Segoe UI", 9, "bold"), pady=6, cursor="hand2"
        )
        btn_hide.pack(fill="x", pady=(6, 0))

    def on_setting_change(self):
        current_settings["auto_clip_enabled"] = self.auto_clip_var.get()
        current_settings["play_sound"] = self.sound_var.get()
        current_settings["bg_detection_mode"] = self.bg_mode_var.get()
        current_settings["ink_mode"] = self.ink_mode_var.get()
        current_settings["auto_crop"] = self.crop_var.get()
        save_settings(current_settings)
        self.sync_ui()

    def on_slider_change(self, _=None):
        if not hasattr(self, 'sens_scale') or not hasattr(self, 'soft_scale'):
            return
        current_settings["sensitivity"] = self.sens_scale.get()
        current_settings["softness"] = self.soft_scale.get()
        if hasattr(self, 'sens_val_lbl'):
            self.sens_val_lbl.config(text=f"{self.sens_scale.get()}")
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

    app.show_window()
    root.mainloop()
