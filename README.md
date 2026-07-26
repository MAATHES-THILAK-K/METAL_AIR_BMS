# 🔋 Metal-Air Single Cell Battery Management System (BMS)

![License: PolyForm Noncommercial 1.0.0](https://img.shields.io/badge/License-PolyForm%20Noncommercial%201.0.0-blue)
![Firmware Version](https://img.shields.io/badge/Firmware-v3.2-green)
![Project Status](https://img.shields.io/badge/Status-Active-brightgreen)

<p align="center">
  <img src="https://github.com/MAATHES-THILAK-K/METAL_AIR_BMS/blob/main/IMAGES/ISOMETRIC.png" alt="PCB Render" width="700">
</p>

---

## 📖 Overview

The **Metal-Air Single Cell Battery Management System (BMS)** is a custom embedded system designed to safely monitor and protect a single-cell Metal-Air battery. The project is built around the **STM32G0B1CBT6** microcontroller and provides real-time monitoring of battery voltage, current, temperature, and hydrogen gas concentration while implementing multiple protection mechanisms to ensure reliable battery operation.

The repository contains the complete embedded firmware, hardware design files, Python monitoring application, and supporting documentation required to reproduce the project.

---

## ✨ Key Features

* STM32G0B1CBT6 Microcontroller
* INA226 High-Precision Current & Bus Voltage Monitoring
* NTC Thermistor-Based Temperature Measurement
* Hydrogen Gas Sensor Monitoring
* Real-Time Fault Detection
* Over-Current Protection (OCP)
* Under-Voltage Protection (UVP)
* Over-Temperature Protection (OTP)
* UART Communication
* Python-Based Monitoring & Data Logging
* Audible Fault Indication (Buzzer)
* MOSFET-Based Load Disconnect

---

## 🏗️ System Architecture

```text
                  +----------------------------+
                  |    Metal-Air Battery       |
                  +-------------+--------------+
                                |
                 +--------------v--------------+
                 |       STM32G0B1CBT6         |
                 +--------------+--------------+
                                |
     +------------+-------------+------------+--------------+
     |            |                          |              |
     |            |                          |              |
+----v----+ +-----v-----+            +-------v------+ +-----v------+
| INA226  | | NTC Sensor|            | H₂ Sensor    | | UART Debug |
| Current | |Temperature|            | Monitoring   | | / Logging  |
+---------+ +-----------+            +--------------+ +------------+
       |                                           |
       +----------------------+--------------------+
                              |
                      +-------v--------+
                      | Protection     |
                      | Decision Logic |
                      +-------+--------+
                              |
                   +----------v-----------+
                   | MOSFET Load Control  |
                   +----------------------+
```

---

## 🔧 Hardware

### Microcontroller

* STM32G0B1CBT6

### Sensors

* INA226 Current & Bus Voltage Sensor
* NTC Thermistor
* Hydrogen Gas Sensor

### Output Devices

* Protection MOSFET
* Buzzer

### Communication

* UART Serial Interface

---

## 💻 Firmware

The embedded firmware continuously performs the following tasks:

* Battery Voltage Measurement
* Current Measurement
* Temperature Monitoring
* Hydrogen Gas Detection
* Fault Monitoring
* Protection Decision Making
* MOSFET Control
* UART Data Transmission for Monitoring

---

## 📂 Project Structure

```text
Metal-Air-Single-Cell-BMS/
│
├── Firmware/
│   └── Metal_Air_BMS.ino
│
├── Python/
│   └── BMS_Monitor.py
│
├── Hardware/
│   ├── Schematic.pdf
│   ├── PCB.pdf
│   ├── PCB_Render.png
│   ├── PCB_Top.png
│   ├── PCB_Bottom.png
│   ├── Gerber.zip
│   ├── BOM.xlsx
│   └── Pick_and_Place.csv
│
├── Images/
│   ├── PCB_Render.png
│   ├── Completed_PCB.jpg
│   ├── Wiring_Diagram.png
│   └── Block_Diagram.png
│
├── README.md
├── requirements.txt
└── .gitignore
```

---

## 🚀 Getting Started

### Hardware

1. Assemble the PCB.
2. Connect the Metal-Air battery.
3. Connect the sensors.
4. Verify all wiring before powering the system.

### Firmware

Compile the firmware using the **Arduino IDE** with the **STM32 Arduino Core** installed.

Upload the firmware using:

* ST-Link
* UART Bootloader

---

## 🎯 Applications

* Metal-Air Battery Research
* Battery Safety Systems
* Embedded Systems Development
* Laboratory Testing
* Battery Performance Evaluation
* Educational and Research Projects

---

## 👨‍💻 Author

**KMT**

Electronics and Communication Engineering

Anna University, MIT Campus, Chennai

---
⭐ If you found this project useful, consider giving it a **Star**!
