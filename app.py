import base64
import queue
import threading
import time
import uuid
import zlib
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

import serial
from serial.tools import list_ports


BAUD_RATES = ["9600", "19200", "38400", "57600", "115200"]
MAX_FILE_SIZE = 10 * 1024
CHUNK_SIZE = 48
ACK_TIMEOUT_SECONDS = 1.5
MAX_RETRIES = 3
RECEIVED_DIR = "received_files"

CMD_CHAT = "CHAT"
CMD_FILE_START = "FILE_START"
CMD_FILE_CHUNK = "FILE_CHUNK"
CMD_FILE_END = "FILE_END"
CMD_FILE_ACK = "FILE_ACK"
CMD_FILE_NACK = "FILE_NACK"
CMD_FILE_ABORT = "FILE_ABORT"

TOKEN_START = "START"
TOKEN_END = "END"


def timestamp() -> str:
    return datetime.now().strftime("%H:%M:%S")


def storage_timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def b64_encode(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def b64_decode(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))


def crc32_hex(data: bytes) -> str:
    return f"{zlib.crc32(data) & 0xFFFFFFFF:08X}"


def split_protocol_line(line: str):
    parts = line.split("|")
    return parts[0], parts[1:]


class SerialConnection:
    def __init__(self, role: str):
        self.role = role
        self.serial_port = None
        self.read_thread = None
        self.stop_event = threading.Event()

    @property
    def is_connected(self) -> bool:
        return self.serial_port is not None and self.serial_port.is_open

    def connect(self, port: str, baud_rate: int) -> None:
        if self.is_connected:
            self.disconnect()

        self.stop_event.clear()
        self.serial_port = serial.Serial(
            port=port,
            baudrate=baud_rate,
            timeout=0.2,
            write_timeout=1,
        )

    def start_reader(self, callback) -> None:
        if not self.is_connected:
            raise RuntimeError(f"{self.role} is not connected.")

        self.read_thread = threading.Thread(
            target=self._read_loop,
            args=(callback,),
            daemon=True,
        )
        self.read_thread.start()

    def _read_loop(self, callback) -> None:
        while not self.stop_event.is_set():
            try:
                if not self.is_connected:
                    break

                raw = self.serial_port.readline()
                if not raw:
                    continue

                line = raw.decode("utf-8", errors="replace").strip()
                if line:
                    callback(line)
            except serial.SerialException as exc:
                callback(f"[Serial error] {exc}")
                break
            except Exception as exc:  # pragma: no cover - UI safety fallback
                callback(f"[Unexpected error] {exc}")
                break

    def send_line(self, line: str) -> None:
        if not self.is_connected:
            raise RuntimeError(f"{self.role} is not connected.")

        payload = f"{line}\n".encode("utf-8")
        written = self.serial_port.write(payload)
        self.serial_port.flush()

        if written != len(payload):
            raise serial.SerialTimeoutException("Only part of the message was written to the serial port.")

    def disconnect(self) -> None:
        self.stop_event.set()

        if self.serial_port is not None:
            try:
                if self.serial_port.is_open:
                    self.serial_port.close()
            finally:
                self.serial_port = None


class LiFiChatApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Li-Fi Communication Demo - Phase 2")
        self.root.geometry("1280x820")
        self.root.minsize(1120, 720)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.is_closing = False

        self.tx_connection = SerialConnection("Transmitter")
        self.rx_connection = SerialConnection("Receiver")
        self.tx_queue = queue.Queue()
        self.rx_queue = queue.Queue()

        # -------------------- NEW FILE TRANSFER SECTION: sender state --------------------
        self.outgoing_transfer = None

        # -------------------- NEW FILE TRANSFER SECTION: receiver state --------------------
        self.incoming_transfer = None

        self._configure_style()
        self._build_ui()
        self.refresh_ports()
        self.process_serial_queues()

    def _configure_style(self) -> None:
        style = ttk.Style()
        style.theme_use("clam")

        self.colors = {
            "page": "#0b1726",
            "page_alt": "#11233a",
            "hero": "#07111d",
            "hero_text": "#f7fbff",
            "panel": "#f6efe5",
            "panel_alt": "#fffaf4",
            "panel_edge": "#e3d4c2",
            "text": "#162535",
            "muted": "#6c5f54",
            "accent": "#d96c3d",
            "accent_dark": "#b6542a",
            "accent_soft": "#f3c3ae",
            "signal": "#1f9d8b",
            "signal_soft": "#bde9df",
            "danger": "#b94e48",
            "input": "#fffdf8",
        }

        self.root.configure(bg=self.colors["page"])

        style.configure("TFrame", background=self.colors["page"])
        style.configure("Toolbar.TFrame", background=self.colors["page"])
        style.configure("Inner.TFrame", background=self.colors["panel"])
        style.configure("HeroTitle.TLabel", font=("Georgia", 24, "bold"), background=self.colors["hero"], foreground=self.colors["hero_text"])
        style.configure("HeroSubtitle.TLabel", font=("Segoe UI", 10), background=self.colors["hero"], foreground="#d5e3f2")
        style.configure("Card.TLabelframe", background=self.colors["panel"], borderwidth=1, relief="solid", bordercolor=self.colors["panel_edge"])
        style.configure("Card.TLabelframe.Label", font=("Georgia", 13, "bold"), background=self.colors["panel"], foreground=self.colors["text"])
        style.configure("Field.TLabel", background=self.colors["panel"], foreground=self.colors["muted"], font=("Segoe UI", 9, "bold"))
        style.configure("PanelText.TLabel", background=self.colors["panel"], foreground=self.colors["text"], font=("Segoe UI", 10))
        style.configure("Status.TLabel", background=self.colors["panel"], foreground=self.colors["accent_dark"], font=("Segoe UI", 10, "bold"))
        style.configure("Progress.TLabel", background=self.colors["panel"], foreground=self.colors["signal"], font=("Segoe UI", 10, "bold"))

        style.configure("TButton", font=("Segoe UI", 10), padding=(12, 8), background=self.colors["panel_alt"], foreground=self.colors["text"], bordercolor=self.colors["panel_edge"])
        style.map("TButton", background=[("active", "#f8efe7")], bordercolor=[("active", self.colors["accent"])])

        style.configure("Accent.TButton", font=("Segoe UI", 10, "bold"), padding=(14, 9), background=self.colors["accent"], foreground="#ffffff", bordercolor=self.colors["accent"])
        style.map("Accent.TButton", background=[("active", self.colors["accent_dark"])], bordercolor=[("active", self.colors["accent_dark"])])

        style.configure("Signal.TButton", font=("Segoe UI", 10, "bold"), padding=(14, 9), background=self.colors["signal"], foreground="#ffffff", bordercolor=self.colors["signal"])
        style.map("Signal.TButton", background=[("active", "#177567")], bordercolor=[("active", "#177567")])

        style.configure("Warn.TButton", font=("Segoe UI", 10, "bold"), padding=(14, 9), background="#fff2ea", foreground=self.colors["danger"], bordercolor="#efb6aa")
        style.map("Warn.TButton", background=[("active", "#ffe8e1")], bordercolor=[("active", self.colors["danger"])])

        style.configure("TCombobox", fieldbackground=self.colors["input"], background=self.colors["input"], foreground=self.colors["text"], bordercolor=self.colors["panel_edge"], lightcolor=self.colors["panel_edge"], darkcolor=self.colors["panel_edge"], arrowsize=14, padding=6)
        style.configure("TEntry", fieldbackground=self.colors["input"], foreground=self.colors["text"], bordercolor=self.colors["panel_edge"], lightcolor=self.colors["panel_edge"], darkcolor=self.colors["panel_edge"], padding=8)
        style.configure("Transfer.Horizontal.TProgressbar", troughcolor="#ead8c4", background=self.colors["signal"], bordercolor=self.colors["panel_edge"], lightcolor=self.colors["signal"], darkcolor=self.colors["signal"])

    def _build_ui(self) -> None:
        container = ttk.Frame(self.root, padding=18, style="Toolbar.TFrame")
        container.pack(fill="both", expand=True)

        hero = tk.Frame(container, bg=self.colors["hero"], bd=0, highlightthickness=0, padx=26, pady=22)
        hero.pack(fill="x", pady=(0, 14))

        hero_top = tk.Frame(hero, bg=self.colors["hero"])
        hero_top.pack(fill="x")

        beacon = tk.Canvas(hero_top, width=74, height=74, bg=self.colors["hero"], highlightthickness=0)
        beacon.pack(side="left", padx=(0, 18))
        beacon.create_oval(20, 20, 54, 54, fill=self.colors["accent"], outline="")
        beacon.create_oval(11, 11, 63, 63, outline=self.colors["accent_soft"], width=2)
        beacon.create_oval(3, 3, 71, 71, outline="#31506f", width=2)

        hero_text = tk.Frame(hero_top, bg=self.colors["hero"])
        hero_text.pack(side="left", fill="both", expand=True)

        ttk.Label(hero_text, text="Li-Fi Communication Project", style="HeroTitle.TLabel").pack(anchor="w")
        ttk.Label(
            hero_text,
            text="Phase 2 adds reliable small-file transfer over serial with chunk framing, CRC checks, and ACK/NACK retries.",
            style="HeroSubtitle.TLabel",
        ).pack(anchor="w", pady=(6, 14))

        hero_stats = tk.Frame(hero, bg=self.colors["hero"])
        hero_stats.pack(fill="x")

        self.tx_summary_var = tk.StringVar(value="Transmitter offline")
        self.rx_summary_var = tk.StringVar(value="Receiver offline")

        self._build_summary_chip(hero_stats, "TX", self.tx_summary_var, self.colors["signal"], self.colors["signal_soft"]).pack(side="left", padx=(0, 12))
        self._build_summary_chip(hero_stats, "RX", self.rx_summary_var, self.colors["accent"], self.colors["accent_soft"]).pack(side="left")

        action_bar = ttk.Frame(container, style="Toolbar.TFrame")
        action_bar.pack(fill="x", pady=(0, 14))
        ttk.Button(action_bar, text="Refresh Ports", command=self.refresh_ports).pack(side="left")
        ttk.Button(action_bar, text="Clear Chat", style="Warn.TButton", command=self.clear_chat).pack(side="left", padx=(10, 0))

        panels = ttk.Frame(container)
        panels.pack(fill="both", expand=True)
        panels.columnconfigure(0, weight=1)
        panels.columnconfigure(1, weight=1)
        panels.rowconfigure(0, weight=1)

        self.tx_frame = ttk.LabelFrame(panels, text="Transmitter Panel", style="Card.TLabelframe", padding=16)
        self.tx_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 8))

        self.rx_frame = ttk.LabelFrame(panels, text="Receiver Panel", style="Card.TLabelframe", padding=16)
        self.rx_frame.grid(row=0, column=1, sticky="nsew", padx=(8, 0))

        self.tx_content = self._create_scrollable_panel(self.tx_frame)
        self.rx_content = self._create_scrollable_panel(self.rx_frame)

        self._build_transmitter_panel()
        self._build_receiver_panel()

    def _create_scrollable_panel(self, parent):
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(0, weight=1)

        body = ttk.Frame(parent, style="Inner.TFrame")
        body.grid(row=0, column=0, sticky="nsew", pady=(8, 0))
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)

        canvas = tk.Canvas(
            body,
            bg=self.colors["panel"],
            highlightthickness=0,
            bd=0,
            relief="flat",
        )
        scrollbar = ttk.Scrollbar(body, orient="vertical", command=canvas.yview)
        content = ttk.Frame(canvas, style="Inner.TFrame")

        window_id = canvas.create_window((0, 0), window=content, anchor="nw")

        def _sync_scrollregion(event=None):
            canvas.configure(scrollregion=canvas.bbox("all"))

        def _resize_content(event):
            canvas.itemconfigure(window_id, width=event.width)

        def _on_mousewheel(event):
            delta = 0
            if hasattr(event, "delta") and event.delta:
                delta = int(-1 * (event.delta / 120))
            elif getattr(event, "num", None) == 4:
                delta = -1
            elif getattr(event, "num", None) == 5:
                delta = 1

            if delta:
                canvas.yview_scroll(delta, "units")

        content.bind("<Configure>", _sync_scrollregion)
        canvas.bind("<Configure>", _resize_content)
        canvas.bind("<Enter>", lambda event: canvas.bind_all("<MouseWheel>", _on_mousewheel))
        canvas.bind("<Leave>", lambda event: canvas.unbind_all("<MouseWheel>"))
        canvas.bind_all("<Button-4>", _on_mousewheel)
        canvas.bind_all("<Button-5>", _on_mousewheel)
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")

        return content

    def _build_summary_chip(self, parent, label: str, variable: tk.StringVar, accent: str, soft_bg: str):
        chip = tk.Frame(parent, bg=soft_bg, padx=12, pady=10, highlightthickness=0, bd=0)
        tk.Label(chip, text=label, bg=accent, fg="#ffffff", font=("Segoe UI", 9, "bold"), padx=8, pady=3).pack(side="left")
        tk.Label(chip, textvariable=variable, bg=soft_bg, fg=self.colors["text"], font=("Segoe UI", 10, "bold"), padx=10).pack(side="left")
        return chip

    def _build_transmitter_panel(self) -> None:
        self.tx_content.columnconfigure(0, weight=1)
        self.tx_content.rowconfigure(7, weight=1)

        tx_header = tk.Frame(self.tx_content, bg=self.colors["panel"], highlightthickness=0)
        tx_header.grid(row=0, column=0, sticky="ew", pady=(0, 14))
        tk.Label(tx_header, text="TRANSMIT", bg=self.colors["accent"], fg="#ffffff", font=("Segoe UI", 9, "bold"), padx=10, pady=4).pack(anchor="w")

        ttk.Label(
            self.tx_content,
            text="Send live text or small files to the transmitter microcontroller. Files are chunked, checksummed \nand retried on NACK/timeouts.",
            style="PanelText.TLabel",
        ).grid(row=1, column=0, sticky="ew", pady=(0, 14))

        controls = ttk.Frame(self.tx_content, style="Inner.TFrame")
        controls.grid(row=2, column=0, sticky="ew")
        ttk.Label(controls, text="SERIAL PORT", style="Field.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 6), pady=(0, 8))
        ttk.Label(controls, text="BAUD RATE", style="Field.TLabel").grid(row=0, column=1, sticky="w", padx=(0, 6), pady=(0, 8))

        self.tx_port_var = tk.StringVar()
        self.tx_baud_var = tk.StringVar(value="9600")
        self.tx_status_var = tk.StringVar(value="Disconnected")
        self.transfer_status_var = tk.StringVar(value="No file transfer in progress.")
        self.transfer_progress_var = tk.DoubleVar(value=0)

        self.tx_port_combo = ttk.Combobox(controls, textvariable=self.tx_port_var, state="readonly", width=18, font=("Segoe UI", 10))
        self.tx_port_combo.grid(row=1, column=0, sticky="ew", padx=(0, 10))
        self.tx_baud_combo = ttk.Combobox(controls, textvariable=self.tx_baud_var, values=BAUD_RATES, state="readonly", width=14, font=("Segoe UI", 10))
        self.tx_baud_combo.grid(row=1, column=1, sticky="ew", padx=(0, 10))
        self.tx_connect_button = ttk.Button(controls, text="Connect", style="Accent.TButton", command=self.toggle_tx_connection)
        self.tx_connect_button.grid(row=1, column=2, sticky="ew")
        controls.columnconfigure(0, weight=1)
        controls.columnconfigure(1, weight=1)

        ttk.Label(self.tx_content, textvariable=self.tx_status_var, style="Status.TLabel").grid(row=3, column=0, sticky="w", pady=(10, 14))

        input_frame = ttk.Frame(self.tx_content, style="Inner.TFrame")
        input_frame.grid(row=4, column=0, sticky="ew", pady=(0, 14))
        input_frame.columnconfigure(0, weight=1)

        ttk.Label(input_frame, text="MESSAGE INPUT", style="Field.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 8))
        self.message_var = tk.StringVar()
        self.message_entry = ttk.Entry(input_frame, textvariable=self.message_var, font=("Segoe UI", 11))
        self.message_entry.grid(row=1, column=0, sticky="ew", padx=(0, 10))
        self.message_entry.bind("<Return>", self.send_message)
        ttk.Button(input_frame, text="Send Message", style="Accent.TButton", command=self.send_message).grid(row=1, column=1, sticky="ew")

        file_frame = ttk.Frame(self.tx_content, style="Inner.TFrame")
        file_frame.grid(row=5, column=0, sticky="ew", pady=(0, 14))
        file_frame.columnconfigure(0, weight=1)

        ttk.Label(file_frame, text="FILE TRANSFER", style="Field.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 8))
        ttk.Button(file_frame, text="Send File", style="Signal.TButton", command=self.select_and_send_file).grid(row=1, column=0, sticky="w")
        ttk.Label(file_frame, textvariable=self.transfer_status_var, style="Progress.TLabel").grid(row=2, column=0, sticky="w", pady=(10, 8))
        self.transfer_progress = ttk.Progressbar(file_frame, maximum=100, variable=self.transfer_progress_var, style="Transfer.Horizontal.TProgressbar")
        self.transfer_progress.grid(row=3, column=0, sticky="ew")

        separator = tk.Frame(self.tx_content, bg=self.colors["panel_edge"], height=1)
        separator.grid(row=6, column=0, sticky="ew", pady=(0, 14))

        log_section = ttk.Frame(self.tx_content, style="Inner.TFrame")
        log_section.grid(row=7, column=0, sticky="nsew")
        log_section.columnconfigure(0, weight=1)
        log_section.rowconfigure(1, weight=1)

        ttk.Label(log_section, text="SENT MESSAGES LOG", style="Field.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 8))
        tx_log_shell = tk.Frame(log_section, bg=self.colors["page_alt"], bd=0, highlightthickness=0, padx=10, pady=10)
        tx_log_shell.grid(row=1, column=0, sticky="nsew")
        tx_log_shell.grid_columnconfigure(0, weight=1)
        tx_log_shell.grid_rowconfigure(0, weight=1)
        self.tx_log = scrolledtext.ScrolledText(
            tx_log_shell,
            wrap="word",
            height=22,
            font=("Consolas", 12),
            bg="#101c2c",
            fg="#ecf4fb",
            insertbackground="#ecf4fb",
            relief="flat",
            borderwidth=0,
            padx=14,
            pady=14,
        )
        self.tx_log.grid(row=0, column=0, sticky="nsew")
        self.tx_log.configure(state="disabled")

    def _build_receiver_panel(self) -> None:
        self.rx_content.columnconfigure(0, weight=1)

        rx_header = tk.Frame(self.rx_content, bg=self.colors["panel"], highlightthickness=0)
        rx_header.pack(fill="x", pady=(0, 14))
        tk.Label(rx_header, text="RECEIVE", bg=self.colors["signal"], fg="#ffffff", font=("Segoe UI", 9, "bold"), padx=10, pady=4).pack(anchor="w")

        ttk.Label(
            self.rx_content,
            text="Incoming text chat still works, and Phase 2 reconstructs small files from serial packets and saves them locally.",
            style="PanelText.TLabel",
        ).pack(anchor="w", pady=(0, 14))

        controls = ttk.Frame(self.rx_content, style="Inner.TFrame")
        controls.pack(fill="x")
        ttk.Label(controls, text="SERIAL PORT", style="Field.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 6), pady=(0, 8))
        ttk.Label(controls, text="BAUD RATE", style="Field.TLabel").grid(row=0, column=1, sticky="w", padx=(0, 6), pady=(0, 8))

        self.rx_port_var = tk.StringVar()
        self.rx_baud_var = tk.StringVar(value="9600")
        self.rx_status_var = tk.StringVar(value="Disconnected")
        self.receive_status_var = tk.StringVar(value="Waiting for receiver connection.")

        self.rx_port_combo = ttk.Combobox(controls, textvariable=self.rx_port_var, state="readonly", width=18, font=("Segoe UI", 10))
        self.rx_port_combo.grid(row=1, column=0, sticky="ew", padx=(0, 10))
        self.rx_baud_combo = ttk.Combobox(controls, textvariable=self.rx_baud_var, values=BAUD_RATES, state="readonly", width=14, font=("Segoe UI", 10))
        self.rx_baud_combo.grid(row=1, column=1, sticky="ew", padx=(0, 10))
        self.rx_connect_button = ttk.Button(controls, text="Connect", style="Accent.TButton", command=self.toggle_rx_connection)
        self.rx_connect_button.grid(row=1, column=2, sticky="ew")
        controls.columnconfigure(0, weight=1)
        controls.columnconfigure(1, weight=1)

        ttk.Label(self.rx_content, textvariable=self.rx_status_var, style="Status.TLabel").pack(anchor="w", pady=(10, 6))
        ttk.Label(self.rx_content, textvariable=self.receive_status_var, style="Progress.TLabel").pack(anchor="w", pady=(0, 14))

        ttk.Label(self.rx_content, text="INCOMING MESSAGES AND FILE CONTENT", style="Field.TLabel").pack(anchor="w", pady=(0, 8))
        rx_display_shell = tk.Frame(self.rx_content, bg=self.colors["signal_soft"], bd=0, highlightthickness=0, padx=10, pady=10)
        rx_display_shell.pack(fill="x", pady=(0, 12))
        self.rx_display = scrolledtext.ScrolledText(
            rx_display_shell,
            wrap="word",
            height=12,
            font=("Segoe UI", 11),
            bg="#f7fffd",
            fg=self.colors["text"],
            insertbackground=self.colors["text"],
            relief="flat",
            borderwidth=0,
            padx=12,
            pady=12,
        )
        self.rx_display.pack(fill="x")
        self.rx_display.configure(state="disabled")

        ttk.Label(self.rx_content, text="RECEIVED MESSAGES LOG", style="Field.TLabel").pack(anchor="w", pady=(0, 8))
        rx_log_shell = tk.Frame(self.rx_content, bg=self.colors["page_alt"], bd=0, highlightthickness=0, padx=10, pady=10)
        rx_log_shell.pack(fill="both", expand=True)
        self.rx_log = scrolledtext.ScrolledText(
            rx_log_shell,
            wrap="word",
            height=14,
            font=("Consolas", 10),
            bg="#101c2c",
            fg="#ecf4fb",
            insertbackground="#ecf4fb",
            relief="flat",
            borderwidth=0,
            padx=12,
            pady=12,
        )
        self.rx_log.pack(fill="both", expand=True)
        self.rx_log.configure(state="disabled")

    def refresh_ports(self) -> None:
        ports = [port.device for port in list_ports.comports()]
        if not ports:
            ports = ["No ports found"]

        self.tx_port_combo["values"] = ports
        self.rx_port_combo["values"] = ports

        if self.tx_port_var.get() not in ports:
            self.tx_port_var.set(ports[0])
        if self.rx_port_var.get() not in ports:
            self.rx_port_var.set(ports[0])

    def toggle_tx_connection(self) -> None:
        if self.tx_connection.is_connected:
            self.tx_connection.disconnect()
            self.outgoing_transfer = None
            self.tx_status_var.set("Disconnected")
            self.tx_summary_var.set("Transmitter offline")
            self.tx_connect_button.configure(text="Connect")
            self.transfer_progress_var.set(0)
            self._set_transfer_idle("No file transfer in progress.")
            self._append_text(self.tx_log, f"[{timestamp()}] Disconnected from transmitter port.\n")
            return

        port = self.tx_port_var.get()
        if port == "No ports found":
            messagebox.showerror("Connection Error", "No serial ports were detected. Connect your transmitter board and refresh ports.")
            return

        try:
            self.tx_connection.connect(port, int(self.tx_baud_var.get()))
            self.tx_connection.start_reader(self._queue_tx_line)
            self.tx_status_var.set(f"Connected to {port} @ {self.tx_baud_var.get()} baud")
            self.tx_summary_var.set(f"{port} ready at {self.tx_baud_var.get()} baud")
            self.tx_connect_button.configure(text="Disconnect")
            self._append_text(self.tx_log, f"[{timestamp()}] Connected to transmitter port {port}.\n")
        except Exception as exc:
            messagebox.showerror("Connection Error", f"Unable to connect to transmitter port.\n\n{exc}")

    def toggle_rx_connection(self) -> None:
        if self.rx_connection.is_connected:
            self.rx_connection.disconnect()
            self.incoming_transfer = None
            self.rx_status_var.set("Disconnected")
            self.rx_summary_var.set("Receiver offline")
            self.rx_connect_button.configure(text="Connect")
            self.receive_status_var.set("Waiting for receiver connection.")
            self._append_text(self.rx_log, f"[{timestamp()}] Disconnected from receiver port.\n")
            return

        port = self.rx_port_var.get()
        if port == "No ports found":
            messagebox.showerror("Connection Error", "No serial ports were detected. Connect your receiver board and refresh ports.")
            return

        try:
            self.rx_connection.connect(port, int(self.rx_baud_var.get()))
            self.rx_connection.start_reader(self._queue_rx_line)
            self.rx_status_var.set(f"Connected to {port} @ {self.rx_baud_var.get()} baud")
            self.rx_summary_var.set(f"{port} listening at {self.rx_baud_var.get()} baud")
            self.rx_connect_button.configure(text="Disconnect")
            self.receive_status_var.set("Ready to receive text or file packets.")
            self._append_text(self.rx_log, f"[{timestamp()}] Connected to receiver port {port}.\n")
        except Exception as exc:
            messagebox.showerror("Connection Error", f"Unable to connect to receiver port.\n\n{exc}")

    def send_message(self, event=None):
        message = self.message_var.get().strip()
        if not message:
            messagebox.showwarning("Empty Message", "Enter a text message before sending.")
            return "break" if event is not None else None

        if not self.tx_connection.is_connected:
            messagebox.showerror("Send Error", "Transmitter is not connected. Connect a transmitter serial port first.")
            return "break" if event is not None else None

        try:
            packet = self._build_chat_packet(message)
            self.tx_connection.send_line(packet)
            self._append_text(self.tx_log, f"[{timestamp()}] TX Chat: {message}\n")
            self.message_var.set("")
        except Exception as exc:
            self._handle_tx_send_error(exc)

        return "break" if event is not None else None

    # -------------------- NEW FILE TRANSFER SECTION: transmit workflow --------------------
    def select_and_send_file(self) -> None:
        if not self.tx_connection.is_connected:
            messagebox.showerror("File Transfer Error", "Connect the transmitter port before sending a file.")
            return

        if self.outgoing_transfer is not None:
            messagebox.showwarning("Transfer Busy", "A file transfer is already in progress.")
            return

        file_path = filedialog.askopenfilename(
            title="Select a small text file",
            filetypes=[("Text files", "*.txt *.csv *.log *.json *.md"), ("All files", "*.*")],
        )
        if not file_path:
            return

        try:
            data = Path(file_path).read_bytes()
        except Exception as exc:
            messagebox.showerror("File Error", f"Unable to read the file.\n\n{exc}")
            return

        if len(data) == 0:
            messagebox.showwarning("Empty File", "Choose a file that contains some text.")
            return

        if len(data) > MAX_FILE_SIZE:
            messagebox.showerror("File Too Large", f"Phase 2 supports files up to {MAX_FILE_SIZE} bytes.")
            return

        transfer_id = uuid.uuid4().hex[:8].upper()
        chunks = [data[i:i + CHUNK_SIZE] for i in range(0, len(data), CHUNK_SIZE)]
        file_name = Path(file_path).name
        file_crc = crc32_hex(data)

        self.outgoing_transfer = {
            "id": transfer_id,
            "file_name": file_name,
            "file_bytes": data,
            "file_size": len(data),
            "file_crc": file_crc,
            "chunks": chunks,
            "chunk_count": len(chunks),
            "phase": "start",
            "waiting_token": None,
            "last_packet": None,
            "deadline": 0.0,
            "retries": 0,
        }

        self.transfer_progress_var.set(0)
        self.transfer_status_var.set(f"Sending {file_name} ({len(data)} bytes)...")
        self._append_text(self.tx_log, f"[{timestamp()}] File queued: {file_name} ({len(data)} bytes, {len(chunks)} chunks).\n")
        self._send_current_transfer_packet()

    def _send_current_transfer_packet(self) -> None:
        if self.outgoing_transfer is None:
            return

        transfer = self.outgoing_transfer
        transfer_id = transfer["id"]

        if transfer["phase"] == "start":
            packet = self._build_file_start_packet(
                transfer_id,
                transfer["file_name"],
                transfer["file_size"],
                transfer["chunk_count"],
                transfer["file_crc"],
            )
            waiting_token = TOKEN_START
            status = f"Sending header for {transfer['file_name']}..."
        elif transfer["phase"] == "chunk":
            index = transfer["next_chunk_index"]
            chunk = transfer["chunks"][index]
            packet = self._build_file_chunk_packet(transfer_id, index, chunk)
            waiting_token = str(index)
            status = f"Sending chunk {index + 1} of {transfer['chunk_count']}..."
        elif transfer["phase"] == "end":
            packet = self._build_file_end_packet(transfer_id, transfer["chunk_count"], transfer["file_crc"])
            waiting_token = TOKEN_END
            status = f"Finalizing {transfer['file_name']}..."
        else:
            return

        try:
            self.tx_connection.send_line(packet)
        except Exception as exc:
            self._fail_outgoing_transfer(f"Transfer failed while writing to serial: {exc}")
            self._handle_tx_send_error(exc, show_dialog=False)
            return

        transfer["waiting_token"] = waiting_token
        transfer["last_packet"] = packet
        transfer["deadline"] = time.monotonic() + ACK_TIMEOUT_SECONDS
        transfer["retries"] = 0
        self.transfer_status_var.set(status)

    def _retry_current_transfer_packet(self, reason: str) -> None:
        transfer = self.outgoing_transfer
        if transfer is None:
            return

        if transfer["retries"] >= MAX_RETRIES:
            self._fail_outgoing_transfer(f"Transfer stopped after repeated failures: {reason}")
            return

        transfer["retries"] += 1
        transfer["deadline"] = time.monotonic() + ACK_TIMEOUT_SECONDS
        self.transfer_status_var.set(f"{reason} Retrying ({transfer['retries']}/{MAX_RETRIES})...")

        try:
            self.tx_connection.send_line(transfer["last_packet"])
        except Exception as exc:
            self._fail_outgoing_transfer(f"Retry failed: {exc}")
            self._handle_tx_send_error(exc, show_dialog=False)

    def _handle_transfer_ack(self, transfer_id: str, token: str) -> None:
        transfer = self.outgoing_transfer
        if transfer is None or transfer["id"] != transfer_id or transfer["waiting_token"] != token:
            return

        if token == TOKEN_START:
            transfer["phase"] = "chunk" if transfer["chunk_count"] > 0 else "end"
            transfer["next_chunk_index"] = 0
            self._send_current_transfer_packet()
            return

        if token == TOKEN_END:
            self.transfer_progress_var.set(100)
            self._append_text(
                self.tx_log,
                f"[{timestamp()}] File sent successfully: {transfer['file_name']} ({transfer['file_size']} bytes).\n",
            )
            self._set_transfer_idle(f"File sent successfully: {transfer['file_name']}")
            self.outgoing_transfer = None
            return

        index = int(token)
        completed = index + 1
        transfer["next_chunk_index"] = completed
        progress = (completed / max(1, transfer["chunk_count"])) * 100
        self.transfer_progress_var.set(progress)

        if completed >= transfer["chunk_count"]:
            transfer["phase"] = "end"
        else:
            transfer["phase"] = "chunk"

        self._send_current_transfer_packet()

    def _handle_transfer_nack(self, transfer_id: str, token: str, reason: str) -> None:
        transfer = self.outgoing_transfer
        if transfer is None or transfer["id"] != transfer_id or transfer["waiting_token"] != token:
            return
        self._retry_current_transfer_packet(f"NACK for token {token}: {reason}")

    def _fail_outgoing_transfer(self, message: str) -> None:
        if self.outgoing_transfer is not None:
            transfer = self.outgoing_transfer
            self._append_text(self.tx_log, f"[{timestamp()}] File transfer failed: {transfer['file_name']} - {message}\n")
            self.transfer_progress_var.set(0)
            self.transfer_status_var.set("Transfer failed.")
        self.outgoing_transfer = None
        messagebox.showerror("File Transfer Error", message)

    def _set_transfer_idle(self, status: str) -> None:
        self.transfer_status_var.set(status)
        if self.outgoing_transfer is None:
            self.transfer_progress_var.set(0)

    # -------------------- NEW FILE TRANSFER SECTION: packet builders --------------------
    def _build_chat_packet(self, message: str) -> str:
        return f"{CMD_CHAT}|{b64_encode(message.encode('utf-8'))}"

    def _build_file_start_packet(self, transfer_id: str, file_name: str, file_size: int, chunk_count: int, file_crc: str) -> str:
        safe_name = b64_encode(file_name.encode("utf-8"))
        return f"{CMD_FILE_START}|{transfer_id}|{safe_name}|{file_size}|{CHUNK_SIZE}|{chunk_count}|{file_crc}"

    def _build_file_chunk_packet(self, transfer_id: str, index: int, chunk: bytes) -> str:
        return f"{CMD_FILE_CHUNK}|{transfer_id}|{index}|{crc32_hex(chunk)}|{b64_encode(chunk)}"

    def _build_file_end_packet(self, transfer_id: str, chunk_count: int, file_crc: str) -> str:
        return f"{CMD_FILE_END}|{transfer_id}|{chunk_count}|{file_crc}"

    def _build_ack_packet(self, transfer_id: str, token: str) -> str:
        return f"{CMD_FILE_ACK}|{transfer_id}|{token}"

    def _build_nack_packet(self, transfer_id: str, token: str, reason: str) -> str:
        return f"{CMD_FILE_NACK}|{transfer_id}|{token}|{b64_encode(reason.encode('utf-8'))}"

    def _build_abort_packet(self, transfer_id: str, reason: str) -> str:
        return f"{CMD_FILE_ABORT}|{transfer_id}|{b64_encode(reason.encode('utf-8'))}"

    def _queue_tx_line(self, line: str) -> None:
        self.tx_queue.put((timestamp(), line))

    def _queue_rx_line(self, line: str) -> None:
        self.rx_queue.put((timestamp(), line))

    def process_serial_queues(self) -> None:
        if self.is_closing:
            return

        while not self.tx_queue.empty():
            msg_time, line = self.tx_queue.get_nowait()
            self._handle_tx_line(msg_time, line)

        while not self.rx_queue.empty():
            msg_time, line = self.rx_queue.get_nowait()
            self._handle_rx_line(msg_time, line)

        self._check_transfer_timeout()

        try:
            self.root.after(100, self.process_serial_queues)
        except tk.TclError:
            pass

    def _check_transfer_timeout(self) -> None:
        transfer = self.outgoing_transfer
        if transfer is None or transfer["waiting_token"] is None:
            return
        if time.monotonic() > transfer["deadline"]:
            self._retry_current_transfer_packet(f"ACK timeout for token {transfer['waiting_token']}.")

    def _handle_tx_line(self, msg_time: str, line: str) -> None:
        if line.startswith("[Serial error]") or line.startswith("[Unexpected error]"):
            self.tx_status_var.set("Disconnected due to serial error")
            self.tx_summary_var.set("Transmitter offline")
            self.tx_connect_button.configure(text="Connect")
            self.tx_connection.disconnect()
            self._append_text(self.tx_log, f"[{msg_time}] {line}\n")
            messagebox.showerror("Transmitter Error", line)
            return

        command, fields = split_protocol_line(line)

        if command == CMD_FILE_ACK and len(fields) >= 2:
            transfer_id, token = fields[0], fields[1]
            self._append_text(self.tx_log, f"[{msg_time}] ACK received for {token} ({transfer_id}).\n")
            self._handle_transfer_ack(transfer_id, token)
            return

        if command == CMD_FILE_NACK and len(fields) >= 3:
            transfer_id, token, encoded_reason = fields[0], fields[1], fields[2]
            reason = b64_decode(encoded_reason).decode("utf-8", errors="replace")
            self._append_text(self.tx_log, f"[{msg_time}] NACK received for {token} ({transfer_id}): {reason}\n")
            self._handle_transfer_nack(transfer_id, token, reason)
            return

        if command == CMD_FILE_ABORT and len(fields) >= 2:
            transfer_id, encoded_reason = fields[0], fields[1]
            reason = b64_decode(encoded_reason).decode("utf-8", errors="replace")
            self._append_text(self.tx_log, f"[{msg_time}] Transfer aborted by receiver ({transfer_id}): {reason}\n")
            if self.outgoing_transfer is not None and self.outgoing_transfer["id"] == transfer_id:
                self._fail_outgoing_transfer(f"Receiver aborted the transfer: {reason}")
            return

        if command == CMD_CHAT and fields:
            message = b64_decode(fields[0]).decode("utf-8", errors="replace")
            self._append_text(self.tx_log, f"[{msg_time}] TX Serial Chat Echo: {message}\n")
            return

        self._append_text(self.tx_log, f"[{msg_time}] TX Serial: {line}\n")

    def _handle_rx_line(self, msg_time: str, line: str) -> None:
        if line.startswith("[Serial error]") or line.startswith("[Unexpected error]"):
            self.rx_status_var.set("Disconnected due to serial error")
            self.rx_summary_var.set("Receiver offline")
            self.rx_connect_button.configure(text="Connect")
            self.rx_connection.disconnect()
            self.receive_status_var.set("Receiver stopped due to serial error.")
            self._append_text(self.rx_log, f"[{msg_time}] {line}\n")
            messagebox.showerror("Receiver Error", line)
            return

        command, fields = split_protocol_line(line)

        if command == CMD_CHAT and fields:
            message = b64_decode(fields[0]).decode("utf-8", errors="replace")
            self._append_text(self.rx_display, f"[{msg_time}] RX Chat:\n{message}\n\n")
            self._append_text(self.rx_log, f"[{msg_time}] RX Chat: {message}\n")
            return

        if command == CMD_FILE_START and len(fields) >= 6:
            self._handle_incoming_file_start(msg_time, fields)
            return

        if command == CMD_FILE_CHUNK and len(fields) >= 4:
            self._handle_incoming_file_chunk(msg_time, fields)
            return

        if command == CMD_FILE_END and len(fields) >= 3:
            self._handle_incoming_file_end(msg_time, fields)
            return

        self._append_text(self.rx_display, f"[{msg_time}] RX:\n{line}\n\n")
        self._append_text(self.rx_log, f"[{msg_time}] RX Raw: {line}\n")

    # -------------------- NEW FILE TRANSFER SECTION: receive workflow --------------------
    def _handle_incoming_file_start(self, msg_time: str, fields) -> None:
        transfer_id, encoded_name, file_size_text, chunk_size_text, chunk_count_text, file_crc = fields[:6]

        try:
            file_name = b64_decode(encoded_name).decode("utf-8", errors="replace")
            file_size = int(file_size_text)
            chunk_size = int(chunk_size_text)
            chunk_count = int(chunk_count_text)
        except Exception:
            self._send_receiver_nack(transfer_id, TOKEN_START, "Invalid file header.")
            self._append_text(self.rx_log, f"[{msg_time}] Invalid FILE_START packet.\n")
            return

        self.incoming_transfer = {
            "id": transfer_id,
            "file_name": file_name,
            "file_size": file_size,
            "chunk_size": chunk_size,
            "chunk_count": chunk_count,
            "file_crc": file_crc,
            "chunks": {},
        }

        self.receive_status_var.set(f"Receiving {file_name} ({file_size} bytes)...")
        self._append_text(self.rx_log, f"[{msg_time}] File incoming: {file_name} ({file_size} bytes, {chunk_count} chunks).\n")
        self._send_receiver_ack(transfer_id, TOKEN_START)

    def _handle_incoming_file_chunk(self, msg_time: str, fields) -> None:
        if self.incoming_transfer is None:
            return

        transfer_id, index_text, chunk_crc, encoded_data = fields[:4]
        if transfer_id != self.incoming_transfer["id"]:
            self._send_receiver_nack(transfer_id, index_text, "Unknown transfer id.")
            return

        try:
            index = int(index_text)
            chunk = b64_decode(encoded_data)
        except Exception:
            self._send_receiver_nack(transfer_id, index_text, "Chunk payload could not be decoded.")
            return

        if crc32_hex(chunk) != chunk_crc:
            self._send_receiver_nack(transfer_id, index_text, "Chunk checksum mismatch.")
            self._append_text(self.rx_log, f"[{msg_time}] Corrupted chunk detected at index {index}.\n")
            return

        self.incoming_transfer["chunks"][index] = chunk
        self.receive_status_var.set(
            f"Receiving {self.incoming_transfer['file_name']} ({len(self.incoming_transfer['chunks'])}/{self.incoming_transfer['chunk_count']} chunks)..."
        )
        self._append_text(self.rx_log, f"[{msg_time}] Chunk {index + 1} received successfully.\n")
        self._send_receiver_ack(transfer_id, index_text)

    def _handle_incoming_file_end(self, msg_time: str, fields) -> None:
        if self.incoming_transfer is None:
            return

        transfer_id, chunk_count_text, file_crc = fields[:3]
        transfer = self.incoming_transfer
        if transfer_id != transfer["id"]:
            self._send_receiver_nack(transfer_id, TOKEN_END, "Transfer id mismatch.")
            return

        expected_chunks = transfer["chunk_count"]
        received_chunks = len(transfer["chunks"])

        if str(expected_chunks) != chunk_count_text or received_chunks != expected_chunks:
            self._send_receiver_nack(transfer_id, TOKEN_END, "Missing chunks in completed transfer.")
            self._append_text(self.rx_log, f"[{msg_time}] Transfer incomplete. Expected {expected_chunks}, got {received_chunks}.\n")
            return

        ordered = b"".join(transfer["chunks"][index] for index in range(expected_chunks))

        if len(ordered) != transfer["file_size"]:
            self._send_receiver_nack(transfer_id, TOKEN_END, "File size mismatch during reconstruction.")
            self._append_text(self.rx_log, f"[{msg_time}] Reconstructed size mismatch.\n")
            return

        if crc32_hex(ordered) != file_crc or transfer["file_crc"] != file_crc:
            self._send_receiver_nack(transfer_id, TOKEN_END, "Final file checksum mismatch.")
            self._append_text(self.rx_log, f"[{msg_time}] Final checksum mismatch.\n")
            return

        try:
            save_path = self._save_received_file(transfer["file_name"], ordered)
        except Exception as exc:
            self._send_receiver_nack(transfer_id, TOKEN_END, f"Could not save file: {exc}")
            return

        content = ordered.decode("utf-8", errors="replace")
        self._append_text(
            self.rx_display,
            f"[{msg_time}] FILE RECEIVED: {transfer['file_name']} ({transfer['file_size']} bytes)\nSaved to: {save_path}\n\n{content}\n\n",
        )
        self._append_text(
            self.rx_log,
            f"[{msg_time}] File received successfully: {transfer['file_name']} ({transfer['file_size']} bytes) -> {save_path}\n",
        )
        self.receive_status_var.set(f"File received successfully: {transfer['file_name']}")
        self._send_receiver_ack(transfer_id, TOKEN_END)
        self.incoming_transfer = None

    def _send_receiver_ack(self, transfer_id: str, token) -> None:
        if not self.rx_connection.is_connected:
            return
        try:
            self.rx_connection.send_line(self._build_ack_packet(transfer_id, str(token)))
        except Exception as exc:
            self._append_text(self.rx_log, f"[{timestamp()}] Failed to send ACK: {exc}\n")

    def _send_receiver_nack(self, transfer_id: str, token, reason: str) -> None:
        if not self.rx_connection.is_connected:
            return
        try:
            self.rx_connection.send_line(self._build_nack_packet(transfer_id, str(token), reason))
            self.receive_status_var.set(f"Transfer issue: {reason}")
        except Exception as exc:
            self._append_text(self.rx_log, f"[{timestamp()}] Failed to send NACK: {exc}\n")

    def _save_received_file(self, original_name: str, data: bytes) -> str:
        target_dir = Path(RECEIVED_DIR)
        target_dir.mkdir(parents=True, exist_ok=True)

        base_path = target_dir / original_name
        if base_path.exists():
            base_path = target_dir / f"{storage_timestamp()}_{original_name}"

        base_path.write_bytes(data)
        return str(base_path.resolve())

    def _handle_tx_send_error(self, exc: Exception, show_dialog: bool = True) -> None:
        self.tx_connection.disconnect()
        self.outgoing_transfer = None
        self.tx_status_var.set("Disconnected due to send error")
        self.tx_summary_var.set("Transmitter offline")
        self.tx_connect_button.configure(text="Connect")
        self.transfer_progress_var.set(0)
        self.transfer_status_var.set("Transfer interrupted.")
        self._append_text(self.tx_log, f"[{timestamp()}] Send failed: {exc}\n")
        if show_dialog:
            messagebox.showerror("Send Error", f"Unable to send data.\n\n{exc}")

    def clear_chat(self) -> None:
        for widget in (self.tx_log, self.rx_display, self.rx_log):
            widget.configure(state="normal")
            widget.delete("1.0", tk.END)
            widget.configure(state="disabled")

    @staticmethod
    def _append_text(widget: scrolledtext.ScrolledText, text: str) -> None:
        try:
            widget.configure(state="normal")
            widget.insert(tk.END, text)
            widget.see(tk.END)
            widget.configure(state="disabled")
        except tk.TclError:
            pass

    def on_close(self) -> None:
        self.is_closing = True
        self.tx_connection.disconnect()
        self.rx_connection.disconnect()
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    app = LiFiChatApp(root)
    app.message_entry.focus_set()
    root.mainloop()


if __name__ == "__main__":
    main()
