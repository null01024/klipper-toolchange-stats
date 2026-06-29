# Support for reading acceleration data from an HXY LIS2DW12TR-compatible chip
#
# Copyright (C) 2023  Zhou.XianMing <zhouxm@biqu3d.com>
# Copyright (C) 2020-2023  Kevin O'Connor <kevin@koconnor.net>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import logging
from . import bus, adxl345, bulk_sensor

# LIS2DW registers
REG_LIS2DW_WHO_AM_I_ADDR = 0x0F
REG_LIS2DW_CTRL_REG1_ADDR = 0x20
REG_LIS2DW_CTRL_REG2_ADDR = 0x21
REG_LIS2DW_CTRL_REG3_ADDR = 0x22
REG_LIS2DW_CTRL_REG4_ADDR = 0x23
REG_LIS2DW_CTRL_REG5_ADDR = 0x24
REG_LIS2DW_CTRL_REG6_ADDR = 0x25
REG_LIS2DW_STATUS_REG_ADDR = 0x27
REG_LIS2DW_OUT_XL_ADDR = 0x28
REG_LIS2DW_OUT_XH_ADDR = 0x29
REG_LIS2DW_OUT_YL_ADDR = 0x2A
REG_LIS2DW_OUT_YH_ADDR = 0x2B
REG_LIS2DW_OUT_ZL_ADDR = 0x2C
REG_LIS2DW_OUT_ZH_ADDR = 0x2D
REG_LIS2DW_FIFO_CTRL   = 0x2E
REG_LIS2DW_FIFO_SAMPLES = 0x2F
REG_MOD_READ = 0x80

LIS2DW_HXY_DEV_ID = 0x11

LIS_I2C_ADDR = 0x19

# Right shift for left justified registers.
FREEFALL_ACCEL = 9.80665
LIS2DW_HXY_SCALE = FREEFALL_ACCEL * 1.952 / 4

BATCH_UPDATES = 0.100

LIS2DW_HXY_TYPE = 'LIS2DW_HXY'
MCU_LIS3DH_TYPE = 'LIS3DH'

SPI_SERIAL_TYPE = 'spi'
I2C_SERIAL_TYPE = 'i2c'

# Printer class that controls HXY LIS2DW12TR-compatible chip
class LIS2DWHXY:
    def __init__(self, config):
        self.printer = config.get_printer()
        adxl345.AccelCommandHelper(config, self)
        self.lis_type = LIS2DW_HXY_TYPE
        self.axes_map = adxl345.read_axes_map(config, LIS2DW_HXY_SCALE,
                        LIS2DW_HXY_SCALE, LIS2DW_HXY_SCALE)
        self.data_rate = 1250
        # Check for spi or i2c
        if config.get('cs_pin', None) is not None:
            self.bus_type = SPI_SERIAL_TYPE
        else:
            self.bus_type = I2C_SERIAL_TYPE
        # Setup mcu sensor_lis2dw bulk query code
        if self.bus_type == SPI_SERIAL_TYPE:
            self.bus = bus.MCU_SPI_from_config(config,
                            3, default_speed=5000000)
        else:
            self.bus = bus.MCU_I2C_from_config(config,
                            default_addr=LIS_I2C_ADDR, default_speed=400000)
        self.mcu = mcu = self.bus.get_mcu()
        self.oid = oid = mcu.create_oid()
        self.query_lis2dw_cmd = None
        mcu.add_config_cmd("config_lis2dw oid=%d bus_oid=%d bus_oid_type=%s "
                           "lis_chip_type=%s" % (oid, self.bus.get_oid(),
                            self.bus_type, MCU_LIS3DH_TYPE))
        mcu.add_config_cmd("query_lis2dw oid=%d rest_ticks=0"
                           % (oid,), on_restart=True)
        mcu.register_config_callback(self._build_config)
        # Bulk sample message reading
        chip_smooth = self.data_rate * BATCH_UPDATES * 2
        self.ffreader = bulk_sensor.FixedFreqReader(mcu, chip_smooth, "<hhh")
        self.last_error_count = 0
        # Process messages in batches
        self.batch_bulk = bulk_sensor.BatchBulkHelper(
            self.printer, self._process_batch,
            self._start_measurements, self._finish_measurements, BATCH_UPDATES)
        self.name = config.get_name().split()[-1]
        hdr = ('time', 'x_acceleration', 'y_acceleration', 'z_acceleration')
        self.batch_bulk.add_mux_endpoint("lis2dw_hxy/dump_lis2dw_hxy", "sensor",
                                         self.name, {'header': hdr})
    def _build_config(self):
        cmdqueue = self.bus.get_command_queue()
        self.query_lis2dw_cmd = self.mcu.lookup_command(
            "query_lis2dw oid=%c rest_ticks=%u", cq=cmdqueue)
        self.ffreader.setup_query_command("query_lis2dw_status oid=%c",
                                          oid=self.oid, cq=cmdqueue)
    def read_reg(self, reg):
        if self.bus_type == SPI_SERIAL_TYPE:
            params = self.bus.spi_transfer([reg | REG_MOD_READ, 0x00])
            response = bytearray(params['response'])
            return response[1]
        params = self.bus.i2c_read([reg], 1)
        return bytearray(params['response'])[0]
    def set_reg(self, reg, val, minclock=0):
        if self.bus_type == SPI_SERIAL_TYPE:
            self.bus.spi_send([reg, val & 0xFF], minclock=minclock)
        else:
            self.bus.i2c_write([reg, val & 0xFF], minclock=minclock)
        stored_val = self.read_reg(reg)
        if stored_val != val:
            raise self.printer.command_error(
                    "Failed to set LIS2DW_HXY register [0x%x] to 0x%x: got 0x%x. "
                    "This is generally indicative of connection problems "
                    "(e.g. faulty wiring) or a faulty lis2dw_hxy chip." % (
                        reg, val, stored_val))
    def start_internal_client(self):
        aqh = adxl345.AccelQueryHelper(self.printer)
        self.batch_bulk.add_client(aqh.handle_batch)
        return aqh
    # Measurement decoding
    def _convert_samples(self, samples):
        (x_pos, x_scale), (y_pos, y_scale), (z_pos, z_scale) = self.axes_map
        count = 0
        for ptime, rx, ry, rz in samples:
            raw_xyz = (rx, ry, rz)
            x = round(raw_xyz[x_pos] * x_scale, 6)
            y = round(raw_xyz[y_pos] * y_scale, 6)
            z = round(raw_xyz[z_pos] * z_scale, 6)
            samples[count] = (round(ptime, 6), x, y, z)
            count += 1
    # Start, stop, and process message batches
    def _start_measurements(self):
        # In case of miswiring, testing the device ID prevents treating
        # noise or wrong signal as a correctly initialized device
        dev_id = self.read_reg(REG_LIS2DW_WHO_AM_I_ADDR)
        logging.info("%s_dev_id: %x", self.lis_type, dev_id)
        if dev_id != LIS2DW_HXY_DEV_ID:
            raise self.printer.command_error(
                "Invalid lis2dw_hxy id (got %x vs %x).\n"
                "This is generally indicative of connection problems\n"
                "(e.g. faulty wiring) or a non-HXY lis2dw-compatible chip."
                % (dev_id, LIS2DW_HXY_DEV_ID))
        # HXY LIS2DW12TR-compatible register map follows the LIS3DH-style
        # FIFO/data layout used by the Klipper MCU sensor_lis2dw code.
        # Enable XYZ at 1.25kHz normal mode.
        self.set_reg(REG_LIS2DW_CTRL_REG1_ADDR, 0x97)
        # Disable high-pass/filter routing.
        self.set_reg(REG_LIS2DW_CTRL_REG2_ADDR, 0x00)
        # BDU=1, BLE=0, FS=+-16g, DLPF disabled, 4-wire SPI mode.
        self.set_reg(REG_LIS2DW_CTRL_REG4_ADDR, 0xB0)
        # Enable FIFO.
        self.set_reg(REG_LIS2DW_CTRL_REG5_ADDR, 0x40)
        # Stream mode.
        self.set_reg(REG_LIS2DW_FIFO_CTRL, 0x80)
        # Start bulk reading
        rest_ticks = self.mcu.seconds_to_clock(4. / self.data_rate)
        self.query_lis2dw_cmd.send([self.oid, rest_ticks])
        self.set_reg(REG_LIS2DW_FIFO_CTRL, 0x80)
        logging.info("%s starting '%s' measurements", self.lis_type, self.name)
        # Initialize clock tracking
        self.ffreader.note_start()
        self.last_error_count = 0
    def _finish_measurements(self):
        # Halt bulk reading
        self.set_reg(REG_LIS2DW_FIFO_CTRL, 0x00)
        self.query_lis2dw_cmd.send_wait_ack([self.oid, 0])
        self.ffreader.note_end()
        logging.info("%s finished '%s' measurements", self.lis_type, self.name)
        self.set_reg(REG_LIS2DW_FIFO_CTRL, 0x00)
    def _process_batch(self, eventtime):
        samples = self.ffreader.pull_samples()
        self._convert_samples(samples)
        if not samples:
            return {}
        return {'data': samples, 'errors': self.last_error_count,
                'overflows': self.ffreader.get_last_overflows()}

def load_config(config):
    return LIS2DWHXY(config)

def load_config_prefix(config):
    return LIS2DWHXY(config)
