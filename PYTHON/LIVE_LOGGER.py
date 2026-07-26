import sys
import serial
import csv
from datetime import datetime
from pathlib import Path

from PyQt5 import QtWidgets, QtCore
import pyqtgraph as pg

# ==========================
# Configuration
# ==========================
PORT = "/dev/ttyUSB0"          # Linux
# PORT = "COM5"                 # Windows
BAUDRATE = 115200
SAVE_FOLDER = "DATA_LOGS"

UPDATE_INTERVAL_MS = 1000         # how often (ms) we poll serial + redraw plots
RECONNECT_INTERVAL_MS = 2000    # how often we retry opening the port when disconnected
MAX_SAMPLES = 50000             # safety cap so a multi-hour run doesn't grow forever

BATTERY_WEIGHT_KG = 1.0         # <-- EDIT ME: physical weight of the battery under test (kg)

# Must match the STM32 fault codes exactly (see checkFaults() in the firmware)
FAULT_MESSAGES = {
    0: "No Fault",
    1: "INA226 Alert",
    2: "Bus Voltage",
    3: "Over Current",
    4: "Hydrogen",
    5: "Temperature",
    6: "Under Current",
}

# ==========================
# CSV setup
# ==========================
Path(SAVE_FOLDER).mkdir(exist_ok=True)

filename = datetime.now().strftime("DATA_%d_%m_%Y-%H_%M_%S.csv")
filepath = Path(SAVE_FOLDER) / filename

counter = 1
while filepath.exists():
    filename = datetime.now().strftime(f"DATA_%d_%m_%Y-%H_%M_%S_{counter}.csv")
    filepath = Path(SAVE_FOLDER) / filename
    counter += 1

print(f"Saving to: {filepath}")

csv_file = open(filepath, "w", newline="")
writer = csv.writer(csv_file)
writer.writerow([
    "PC Time",
    "Voltage (V)",
    "Current (A)",
    "Power (W)",
    "Temperature (°C)",
    "Hydrogen (V)",
    "MOSFET",
    "Fault Reason",
    "Fault Text",
    "Energy (Wh)",
    "Capacity (Ah)",
    "Energy Density (Wh/kg)",
])
csv_file.flush()


class _CardFrame(QtWidgets.QFrame):
    """
    A QFrame that can host a small overlay button pinned to its top-right
    corner (fixed padding from the top and right edges). The button floats
    over the card's normal contents and is repositioned automatically
    whenever the card is resized, so it stays put as the layout stretches.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._overlay_btn = None
        self._overlay_margin = 16

    def set_overlay_button(self, btn, margin=16):
        self._overlay_btn = btn
        self._overlay_margin = margin
        btn.setParent(self)
        btn.raise_()
        self._position_overlay()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._position_overlay()

    def _position_overlay(self):
        if self._overlay_btn is None:
            return
        m = self._overlay_margin
        x = self.width() - self._overlay_btn.width() - m
        y = m
        self._overlay_btn.move(x, y)


class LiveGraphWindow(QtWidgets.QMainWindow):
    """
    Main window. A fast QTimer polls the serial port and redraws the plots;
    a slower QTimer retries the connection whenever the port is closed or
    the cable gets unplugged mid-run. Every valid packet is logged to CSV
    exactly like before, plus the derived energy/capacity/energy-density
    figures.
    """

    def __init__(self):
        super().__init__()
        self.setWindowTitle("BMS LIVE LOGGER")
        self.resize(1300, 1500)

        # ---- serial connection state ----
        self.ser = None
        self.connected = False

        # ---- session state (for energy / capacity integration) ----
        self.start_time = None
        self.prev_time = None
        self.prev_power = 0.0
        self.energy_wh = 0.0

        # ---- data buffers -- x-axis is elapsed minutes since first sample ----
        self.t = []
        self.voltage = []
        self.current = []
        self.power = []
        self.temperature = []
        self.hydrogen = []
        self.capacity = []

        # ---- build UI ----
        # Everything lives inside a scroll area so that on smaller/laptop
        # screens, plots that don't fit vertically can be scrolled to
        # instead of being clipped off-screen.
        scroll_area = QtWidgets.QScrollArea()
        scroll_area.setWidgetResizable(True)
        self.setCentralWidget(scroll_area)

        content = QtWidgets.QWidget()
        scroll_area.setWidget(content)
        main_layout = QtWidgets.QVBoxLayout(content)

        self._build_status_bars(main_layout)

        self._build_time_series_plots(main_layout)
        self._build_correlation_plots(main_layout)

        # ---- slide-out settings panel (defaults must mirror the firmware) ----
        self.current_settings = {
            "voltage_cutoff": 0.8,
            "current_upper": 16.0,
            "current_lower": 0.0,
            "temp_lower": 25.0,
            "temp_upper": 45.0,
            "hydrogen_limit": 4.0,
            "data_rate": 1700,
        }
        self.bms_boot_time = None       # set when serial connects; used for the 60s config window
        self.CONFIG_WINDOW_SECONDS = 60  # must match CONFIG_WINDOW_MS in the firmware
        self._build_settings_panel()
        self._position_settings_panel()

        # ---- timers ----
        self.data_timer = QtCore.QTimer()
        self.data_timer.timeout.connect(self.read_serial_and_update)
        self.data_timer.start(UPDATE_INTERVAL_MS)

        self.reconnect_timer = QtCore.QTimer()
        self.reconnect_timer.timeout.connect(self.try_connect)
        self.reconnect_timer.start(RECONNECT_INTERVAL_MS)

        self.try_connect()  # attempt once immediately at startup

    # ------------------------------------------------------------------
    # UI builders
    # ------------------------------------------------------------------
    def _build_status_bars(self, main_layout):
        self.value_labels = {}

        # Row 1 - live sensor readings (6 cards, all equal width/height/gap)
        readings_bar = QtWidgets.QHBoxLayout()
        readings_bar.setSpacing(10)
        metrics = [
            ("voltage", "VOLTAGE", "V"),
            ("current", "CURRENT", "A"),
            ("power", "POWER", "W"),
            ("temperature", "TEMPERATURE", "°C"),
            ("hydrogen", "HYDROGEN", "V"),
        ]

        for key, title, unit in metrics:
            card, value_label = self._make_value_card(title, unit)
            self.value_labels[key] = value_label
            readings_bar.addWidget(card, 1)  # stretch=1 -> every card gets an equal share of the width

        # Energy Density card (contains the menu button) -- same stretch as the rest
        card, value_label = self._make_value_card(
            "ENERGY DENSITY",
            "Wh/kg",
            menu_button=True
        )
        self.value_labels["energy_density"] = value_label
        readings_bar.addWidget(card, 1)
        main_layout.addLayout(readings_bar)

        # Row 2 - system status (3 cards, same even-width/gap treatment)
        status_bar = QtWidgets.QHBoxLayout()
        status_bar.setSpacing(10)
        self.conn_card, self.conn_value_label = self._make_value_card("USB / UART", "")
        self.mosfet_card, self.mosfet_value_label = self._make_value_card("MOSFET", "")
        self.fault_card, self.fault_value_label = self._make_value_card("FAULT", "")
        status_bar.addWidget(self.conn_card, 1)
        status_bar.addWidget(self.mosfet_card, 1)
        status_bar.addWidget(self.fault_card, 1)
        main_layout.addLayout(status_bar)

        # initial states
        self._set_connection_status(False)
        self._update_mosfet_indicator("0")
        self._update_fault_indicator(0)

    def _build_settings_panel(self):
        self.settings_panel = QtWidgets.QFrame(self)
        self.settings_panel.setObjectName("settingsPanel")
        self.settings_panel.setStyleSheet("""
            QFrame#settingsPanel { background-color: #1e1e1e; border: 2px solid #ffffff; }
        """)

        outer = QtWidgets.QVBoxLayout(self.settings_panel)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # ---- header bar ----
        header = QtWidgets.QFrame()
        header.setStyleSheet("""
    QFrame {
        background-color: #2b2b2b;


    }
""")
        header.setFixedHeight(56)
        header_layout = QtWidgets.QHBoxLayout(header)
        header_layout.setContentsMargins(18, 0, 12, 0)

        title = QtWidgets.QLabel("BMS SETTINGS")
        title.setStyleSheet("color: white; font-size: 15px; font-weight: bold; letter-spacing: 1px; background: transparent;")

        close_btn = QtWidgets.QPushButton("✕")
        close_btn.setFixedSize(30, 30)
        close_btn.setCursor(QtCore.Qt.PointingHandCursor)
        close_btn.setStyleSheet("""
            QPushButton { background-color: #3c3c3c; color: #dddddd; border-radius: 15px; font-weight: bold; }
            QPushButton:hover { background-color: #b23b3b; color: white; }
        """)
        close_btn.clicked.connect(self.toggle_settings_panel)

        header_layout.addWidget(title)
        header_layout.addStretch()
        header_layout.addWidget(close_btn)
        outer.addWidget(header)

        # ---- scrollable field area ----
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        scroll.setStyleSheet("background: transparent;")

        body = QtWidgets.QWidget()
        body.setStyleSheet("background: transparent;")
        body_layout = QtWidgets.QVBoxLayout(body)
        body_layout.setContentsMargins(18, 18, 18, 18)
        body_layout.setSpacing(12)

        self.settings_fields = {}
        field_defs = [
            ("voltage_cutoff", "VOLTAGE CUTOFF", "V"),
            ("current_upper", "CURRENT UPPER LIMIT", "A"),
            ("current_lower", "CURRENT LOWER LIMIT", "A"),
            ("temp_lower", "TEMPERATURE LOWER LIMIT", "°C"),
            ("temp_upper", "TEMPERATURE UPPER LIMIT", "°C"),
            ("hydrogen_limit", "HYDROGEN LIMIT", "V"),
            ("data_rate", "DATA TRANSFER RATE", "ms"),
        ]
        for key, label, unit in field_defs:
            card, edit = self._make_setting_card(label, unit)
            self.settings_fields[key] = edit
            body_layout.addWidget(card)

        body_layout.addStretch()
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)

        # ---- footer / save button ----
        footer = QtWidgets.QFrame()
        footer.setStyleSheet("background-color: #1e1e1e; border-top: 1px solid #333;")
        footer_layout = QtWidgets.QVBoxLayout(footer)
        footer_layout.setContentsMargins(18, 14, 18, 18)

        save_btn = QtWidgets.QPushButton("SAVE AND SEND")
        save_btn.setCursor(QtCore.Qt.PointingHandCursor)
        save_btn.setFixedHeight(42)
        save_btn.setStyleSheet("""
            QPushButton {
                background-color: #007acc; color: white; font-weight: bold;
                font-size: 13px; border-radius: 6px; letter-spacing: 0.5px;
            }
            QPushButton:hover { background-color: #0090f0; }
            QPushButton:pressed { background-color: #005f9e; }
        """)
        save_btn.clicked.connect(self.save_and_send_settings)
        footer_layout.addWidget(save_btn)
        outer.addWidget(footer)

        self.settings_panel_open = False

    def _make_setting_card(self, label_text, unit):
        card = QtWidgets.QFrame()
        card.setStyleSheet("QFrame { background-color: #2b2b2b; border-radius: 8px; }")

        vbox = QtWidgets.QVBoxLayout(card)
        vbox.setContentsMargins(12, 10, 12, 10)
        vbox.setSpacing(6)

        label = QtWidgets.QLabel(f"{label_text} ({unit})" if unit else label_text)
        label.setStyleSheet("""
            font-size: 11px; color: #aaaaaa; font-weight: bold;
            letter-spacing: 0.5px; background: transparent;
        """)

        edit = QtWidgets.QLineEdit()
        edit.setStyleSheet("""
            QLineEdit {
                background-color: #1e1e1e; color: white; padding: 8px 10px;
                border-radius: 5px; border: 1px solid #444; font-size: 14px;
            }
            QLineEdit:focus { border: 1px solid #007acc; }
        """)

        vbox.addWidget(label)
        vbox.addWidget(edit)
        return card, edit

    def _panel_width(self):
        return max(int(self.width() * 0.30), 340)

    def _position_settings_panel(self):
        panel_w, panel_h = self._panel_width(), self.height()
        x = (self.width() - panel_w) if self.settings_panel_open else self.width()
        self.settings_panel.setGeometry(x, 0, panel_w, panel_h)
        self.settings_panel.raise_()

    def _config_window_open(self):
        if self.bms_boot_time is None:
            return False
        elapsed = (datetime.now() - self.bms_boot_time).total_seconds()
        return elapsed <= self.CONFIG_WINDOW_SECONDS

    def _show_config_timeout_warning(self):
        box = QtWidgets.QMessageBox(self)
        box.setIcon(QtWidgets.QMessageBox.Critical)
        box.setWindowTitle("Configuration Locked")
        box.setText("                                                WARNING !!")
        box.setInformativeText("                                Timeout error: 60 seconds over.\n                                Reset the BMS to configure.")
        box.setStyleSheet("""
            QMessageBox { background-color: #202020; }
            QLabel { color: #eaeaea; font-size: 13px; }
            QPushButton {
                background-color: #b23b3b; color: white; font-weight: bold;
                padding: 6px 16px; border-radius: 5px; border: none;
            }
            QPushButton:hover { background-color: #cc4444; }
        """)

        # QMessageBox auto-sizes to its text, so setMinimumWidth()/resize()
        # get ignored. Forcing an invisible spacer into its internal grid
        # layout is the reliable way to widen it (bump the width below to
        # taste).
        spacer = QtWidgets.QSpacerItem(
            500, 0, QtWidgets.QSizePolicy.Minimum, QtWidgets.QSizePolicy.Expanding
        )
        layout = box.layout()
        layout.addItem(spacer, layout.rowCount(), 0, 1, layout.columnCount())

        box.exec_()

    def toggle_settings_panel(self):
        opening = not self.settings_panel_open

        if opening and not self._config_window_open():
            self._show_config_timeout_warning()
            return

        panel_w, panel_h = self._panel_width(), self.height()
        start_rect = self.settings_panel.geometry()

        if opening:
            for key, edit in self.settings_fields.items():
                edit.setText(str(self.current_settings[key]))
            self.settings_panel.setGeometry(self.width(), 0, panel_w, panel_h)
            end_rect = QtCore.QRect(self.width() - panel_w, 0, panel_w, panel_h)
            self.settings_panel_open = True
        else:
            end_rect = QtCore.QRect(self.width(), 0, panel_w, panel_h)
            self.settings_panel_open = False

        self.settings_panel.show()
        self.settings_panel.raise_()

        self._panel_anim = QtCore.QPropertyAnimation(self.settings_panel, b"geometry")
        self._panel_anim.setDuration(300)
        self._panel_anim.setStartValue(start_rect)
        self._panel_anim.setEndValue(end_rect)
        self._panel_anim.setEasingCurve(QtCore.QEasingCurve.OutCubic)
        self._panel_anim.start()

    def save_and_send_settings(self):
        if not self._config_window_open():
            self._show_config_timeout_warning()
            self.toggle_settings_panel()  # slide the now-locked panel shut
            return

        try:
            new_values = {
                "voltage_cutoff": float(self.settings_fields["voltage_cutoff"].text()),
                "current_upper": float(self.settings_fields["current_upper"].text()),
                "current_lower": float(self.settings_fields["current_lower"].text()),
                "temp_lower": float(self.settings_fields["temp_lower"].text()),
                "temp_upper": float(self.settings_fields["temp_upper"].text()),
                "hydrogen_limit": float(self.settings_fields["hydrogen_limit"].text()),
                "data_rate": int(float(self.settings_fields["data_rate"].text())),
            }
        except ValueError:
            QtWidgets.QMessageBox.warning(self, "Invalid Input", "Please enter valid numbers in all fields.")
            return

        if not self.connected or self.ser is None:
            QtWidgets.QMessageBox.warning(self, "Not Connected", "Cannot send settings: not connected to STM32.")
            return

        commands = [
            f"SET_V:{new_values['voltage_cutoff']:.3f}\n",
            f"SET_IH:{new_values['current_upper']:.3f}\n",
            f"SET_IL:{new_values['current_lower']:.3f}\n",
            f"SET_TL:{new_values['temp_lower']:.2f}\n",
            f"SET_TH:{new_values['temp_upper']:.2f}\n",
            f"SET_H2:{new_values['hydrogen_limit']:.2f}\n",
            f"SET_RATE:{new_values['data_rate']}\n",
        ]
        try:
            for cmd in commands:
                self.ser.write(cmd.encode("utf-8"))
                self.ser.flush()
                QtCore.QThread.msleep(50)  # let the STM32 drain its RX buffer between lines
            print("Sent updated settings to STM32:", new_values)
        except (serial.SerialException, OSError) as e:
            QtWidgets.QMessageBox.warning(self, "Send Failed", f"Could not send settings: {e}")
            return

        self.current_settings = new_values
        self.toggle_settings_panel()  # slides shut as the values go out

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._position_settings_panel()

    def _build_time_series_plots(self, main_layout):
        grid = QtWidgets.QGridLayout()
        main_layout.addLayout(grid)

        pg.setConfigOptions(antialias=True)

        self.plot_voltage = self._make_time_plot("Voltage", "V")
        self.plot_current = self._make_time_plot("Current", "A")
        self.plot_power = self._make_time_plot("Power", "W")
        self.plot_temp = self._make_time_plot("Temperature", "°C")
        self.plot_hydrogen = self._make_time_plot("Hydrogen", "V")
        # ---------- Fixed Y-axis ranges ----------
        self.plot_voltage.setYRange(0, 4)
        self.plot_current.setYRange(0, 20)
        self.plot_power.setYRange(0, 80)
        self.plot_temp.setYRange(10, 60)
        self.plot_hydrogen.setYRange(0, 5)

        # Disable automatic Y scaling
        self.plot_voltage.enableAutoRange(axis='y', enable=False)
        self.plot_current.enableAutoRange(axis='y', enable=False)
        self.plot_power.enableAutoRange(axis='y', enable=False)
        self.plot_temp.enableAutoRange(axis='y', enable=False)
        self.plot_hydrogen.enableAutoRange(axis='y', enable=False)

        grid.addWidget(self.plot_voltage, 0, 0)
        grid.addWidget(self.plot_current, 0, 1)
        grid.addWidget(self.plot_power, 1, 0)
        grid.addWidget(self.plot_temp, 1, 1)
        grid.addWidget(self.plot_hydrogen, 2, 0, 1, 2)

        self.curve_voltage = self.plot_voltage.plot(pen=pg.mkPen("y", width=2))
        self.curve_current = self.plot_current.plot(pen=pg.mkPen("c", width=2))
        self.curve_power = self.plot_power.plot(pen=pg.mkPen("m", width=2))
        self.curve_temp = self.plot_temp.plot(pen=pg.mkPen("r", width=2))
        self.curve_hydrogen = self.plot_hydrogen.plot(pen=pg.mkPen("g", width=2))

    def _build_correlation_plots(self, main_layout):
        # One full-width ("landscape") plot per row, stacked below the grid above.
        self.plot_v_vs_i = self._make_xy_plot("Voltage vs Current", "Current (A)", "Voltage (V)")
        self.plot_v_vs_t = self._make_xy_plot("Voltage vs Temperature", "Temperature (°C)", "Voltage (V)")
        self.plot_capacity = self._make_time_plot("Capacity", "Ah")

        for plot in (self.plot_v_vs_i, self.plot_v_vs_t, self.plot_capacity):
            main_layout.addWidget(plot)

        self.curve_v_vs_i = self.plot_v_vs_i.plot(
            pen=pg.mkPen("y", width=2)
        )

        self.curve_v_vs_t = self.plot_v_vs_t.plot(
            pen=pg.mkPen("r", width=2)
        )
        self.curve_capacity = self.plot_capacity.plot(pen=pg.mkPen("#38b6ff", width=2))

    def _make_time_plot(self, title, y_label):
        plot = self._make_plot(title, y_label, "Time (minutes)")
        # Major gridline every 30 min, minor (lighter) gridline every 15 min
        plot.getAxis("bottom").setTickSpacing(major=30, minor=15)
        return plot

    def _make_xy_plot(self, title, x_label, y_label):
        return self._make_plot(title, y_label, x_label)

    def _make_plot(self, title, y_label, x_label):
        plot = pg.PlotWidget(title=title)
        plot.setLabel("left", y_label)
        plot.setLabel("bottom", x_label)
        plot.showGrid(x=True, y=True, alpha=0.3)
        plot.setMinimumHeight(350)  # keeps plots readable; scroll area handles overflow
        view_box = plot.getViewBox()
        view_box.setMouseEnabled(x=False, y=False)   # kills mouse-wheel zoom AND click-drag zoom/pan
        plot.setMenuEnabled(False)                    # removes the right-click "view all" menu
        plot.hideButtons()                            # removes the little "A" auto-range button in the corner
        return plot

    def _make_value_card(self, title, unit, menu_button=False):
        # The Energy Density card uses _CardFrame so it can host a pinned
        # overlay button; every other card is a plain QFrame. Either way the
        # title/value layout below is identical, so all cards line up the same.
        card = _CardFrame() if menu_button else QtWidgets.QFrame()
        card.setFrameShape(QtWidgets.QFrame.StyledPanel)
        card.setStyleSheet("QFrame { background-color: #2b2b2b; border-radius: 8px; }")
        # Equal width (grows with its stretch share) and a fixed height so
        # every card in the row is exactly the same size.
        card.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
        card.setFixedHeight(92)
        card.setMinimumWidth(0)

        vbox = QtWidgets.QVBoxLayout(card)
        vbox.setContentsMargins(10, 8, 10, 8)

        title_label = QtWidgets.QLabel(title)
        title_label.setAlignment(QtCore.Qt.AlignCenter)
        title_label.setStyleSheet("font-size: 12px; color: #aaaaaa; font-weight: bold;")
        vbox.addWidget(title_label)

        value_label = QtWidgets.QLabel(f"-- {unit}".strip())
        value_label.setAlignment(QtCore.Qt.AlignCenter)
        value_label.setStyleSheet("font-size: 20px; color: white; font-weight: bold;")
        vbox.addWidget(value_label)

        if menu_button:
            menu_btn = QtWidgets.QPushButton("☰", card)
            menu_btn.setFixedSize(40, 40)
            menu_btn.setCursor(QtCore.Qt.PointingHandCursor)
            menu_btn.setStyleSheet("""
                QPushButton {
                    background-color: #3c3c3c;
                    color: white;
                    border-radius: 10px;
                    border: 1px solid #555;
                    font-size: 28px;
                    font-weight: bold;
                }
                QPushButton:hover {
                    background-color: #555555;
                }
            """)
            menu_btn.clicked.connect(self.toggle_settings_panel)
            card.set_overlay_button(menu_btn, margin=16)  # 16px from the top, 16px from the right

        return card, value_label

    # ------------------------------------------------------------------
    # Serial connection handling
    # ------------------------------------------------------------------
    def try_connect(self):
        if self.connected:
            return
        try:
            self.ser = serial.Serial(PORT, BAUDRATE, timeout=1)
            self.connected = True
            self._set_connection_status(True)
            self.bms_boot_time = datetime.now()  # proxy for BMS power-on, starts the 60s config window
            print(f"Connected to {PORT}")
        except serial.SerialException:
            self.ser = None
            self.connected = False
            self._set_connection_status(False)

    def _handle_disconnect(self):
        print("Serial connection lost.")
        if self.ser is not None:
            try:
                self.ser.close()
            except Exception:
                pass
        self.ser = None
        self.connected = False
        self._set_connection_status(False)

    def _set_connection_status(self, connected):
        if connected:
            self.conn_value_label.setText("CONNECTED")
            self.conn_card.setStyleSheet("QFrame { background-color: #2f7d4f; border-radius: 8px; }")
        else:
            self.conn_value_label.setText("DISCONNECTED")
            self.conn_card.setStyleSheet("QFrame { background-color: #b23b3b; border-radius: 8px; }")

    # ------------------------------------------------------------------
    # Main read/update loop
    # ------------------------------------------------------------------
    def read_serial_and_update(self):
        if self.connected and self.ser is not None:
            try:
                self._drain_serial()
            except (serial.SerialException, OSError):
                self._handle_disconnect()

        # Redraw every tick -- cheap even when no new data arrived
        self.curve_voltage.setData(self.t, self.voltage)
        self.curve_current.setData(self.t, self.current)
        self.curve_power.setData(self.t, self.power)
        self.curve_temp.setData(self.t, self.temperature)
        self.curve_hydrogen.setData(self.t, self.hydrogen)
        self.curve_capacity.setData(self.t, self.capacity)
        self.curve_v_vs_i.setData(self.current, self.voltage)
        self.curve_v_vs_t.setData(self.temperature, self.voltage)

    def _drain_serial(self):
        while self.ser.in_waiting:
            raw = self.ser.readline().decode("utf-8", errors="ignore").strip()
            if not raw:
                continue

            # Firmware diagnostic/confirmation lines ([CFG], [SYS], [ERR], [WARN])
            # ride the same UART but are not telemetry -- show them and move on.
            if raw.startswith("["):
                print("BMS:", raw)
                continue

            # Real telemetry is explicitly tagged "DATA,..." by the firmware.
            if not raw.startswith("DATA,"):
                print("Ignoring non-telemetry line:", raw)
                continue

            values = raw[len("DATA,"):].split(",")

            # Firmware sends 7 fields:
            # bus_V, current_A, power_W, temp_C, h2_V, mosfet, faultReason
            if len(values) != 7:
                print("Invalid packet:", raw)
                continue

            pc_time = datetime.now().strftime("%H:%M:%S.%f")[:-3]

            try:
                voltage = float(values[0])
                current = float(values[1])
                power = float(values[2])
                temperature = float(values[3])
                hydrogen = float(values[4])
                mosfet_raw = values[5].strip()
                fault_code = int(values[6])
            except ValueError:
                print("Could not parse numeric values:", raw)
                continue

            now = datetime.now()
            if self.start_time is None:
                self.start_time = now
                self.prev_time = now
                self.prev_power = power

            elapsed_min = (now - self.start_time).total_seconds() / 60.0

            # Trapezoidal integration of power over time -> energy (Wh)
            dt_hours = (now - self.prev_time).total_seconds() / 3600.0
            self.energy_wh += (self.prev_power + power) / 2.0 * dt_hours
            self.prev_power = power
            self.prev_time = now

            # capacity (Ah) = energy (Wh) / voltage (V)
            capacity_ah = (self.energy_wh / voltage) if voltage > 0.05 else 0.0
            # energy density (Wh/kg) = (voltage x capacity) / weight
            energy_density = (
                (voltage * capacity_ah) / BATTERY_WEIGHT_KG if BATTERY_WEIGHT_KG > 0 else 0.0
            )

            fault_text = FAULT_MESSAGES.get(fault_code, f"Unknown ({fault_code})")

            # Log to CSV
            writer.writerow([
                pc_time, values[0], values[1], values[2], values[3], values[4],
                mosfet_raw, fault_code, fault_text,
                f"{self.energy_wh:.4f}", f"{capacity_ah:.4f}", f"{energy_density:.4f}",
            ])
            csv_file.flush()

            self._append_sample(elapsed_min, voltage, current, power, temperature, hydrogen, capacity_ah)

            self._update_current_values(voltage, current, power, temperature, hydrogen, energy_density)
            self._update_mosfet_indicator(mosfet_raw)
            self._update_fault_indicator(fault_code)

    def _append_sample(self, t, voltage, current, power, temperature, hydrogen, capacity):
        self.t.append(t)
        self.voltage.append(voltage)
        self.current.append(current)
        self.power.append(power)
        self.temperature.append(temperature)
        self.hydrogen.append(hydrogen)
        self.capacity.append(capacity)

        # Safety cap so an extremely long run doesn't grow memory forever
        if len(self.t) > MAX_SAMPLES:
            trim = len(self.t) - MAX_SAMPLES
            del self.t[:trim]
            del self.voltage[:trim]
            del self.current[:trim]
            del self.power[:trim]
            del self.temperature[:trim]
            del self.hydrogen[:trim]
            del self.capacity[:trim]

    # ------------------------------------------------------------------
    # Status card updates
    # ------------------------------------------------------------------
    def _update_current_values(self, voltage, current, power, temperature, hydrogen, energy_density):
        self.value_labels["voltage"].setText(f"{voltage:.3f} V")
        self.value_labels["current"].setText(f"{current:.3f} A")
        self.value_labels["power"].setText(f"{power:.3f} W")
        self.value_labels["temperature"].setText(f"{temperature:.1f} °C")
        self.value_labels["hydrogen"].setText(f"{hydrogen:.2f} V")
        self.value_labels["energy_density"].setText(f"{energy_density:.2f} Wh/kg")

    def _update_mosfet_indicator(self, mosfet_raw):
        is_on = mosfet_raw in ("1", "1.0", "True", "true", "ON", "on")
        if is_on:
            self.mosfet_value_label.setText("CUT OFF")
            self.mosfet_card.setStyleSheet("QFrame { background-color: #b23b3b; border-radius: 8px; }")
        else:
            self.mosfet_value_label.setText("NORMAL")
            self.mosfet_card.setStyleSheet("QFrame { background-color: #2f7d4f; border-radius: 8px; }")

    def _update_fault_indicator(self, fault_code):
        fault_text = FAULT_MESSAGES.get(fault_code, f"Unknown ({fault_code})")
        self.fault_value_label.setText(fault_text)
        color = "#2f7d4f" if fault_code == 0 else "#b23b3b"
        self.fault_card.setStyleSheet(f"QFrame {{ background-color: {color}; border-radius: 8px; }}")

    def closeEvent(self, event):
        self.data_timer.stop()
        self.reconnect_timer.stop()
        if self.ser is not None:
            try:
                self.ser.close()
            except Exception:
                pass
        try:
            csv_file.close()
        except Exception:
            pass
        event.accept()


def main():
    app = QtWidgets.QApplication(sys.argv)
    window = LiveGraphWindow()
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
