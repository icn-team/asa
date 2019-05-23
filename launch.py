#!/usr/bin/env python3

import datetime
import logging
import math
import os
import random
import re
import signal
import sys
import subprocess
import telnetlib
import time

from pyroute2 import IPRoute


def handle_SIGCHLD(signal, frame):
    os.waitpid(-1, os.WNOHANG)


def handle_SIGTERM(signal, frame):
    sys.exit(0)


signal.signal(signal.SIGINT, handle_SIGTERM)
signal.signal(signal.SIGTERM, handle_SIGTERM)
signal.signal(signal.SIGCHLD, handle_SIGCHLD)

TRACE_LEVEL_NUM = 9
logging.addLevelName(TRACE_LEVEL_NUM, "TRACE")


def trace(self, message, *args, **kws):
    # Yes, logger takes its '*args' as 'args'.
    if self.isEnabledFor(TRACE_LEVEL_NUM):
        self._log(TRACE_LEVEL_NUM, message, args, **kws)


logging.Logger.trace = trace

MAX_RETRIES=60

def gen_mac(last_octet=None):
    """ Generate a random MAC address that is in the qemu OUI space and that
        has the given last octet.
    """
    return "52:54:00:%02x:%02x:%02x" % (
            random.randint(0x00, 0xff),
            random.randint(0x00, 0xff),
            last_octet
        )


def run_command(cmd, cwd=None, background=False):
    res = None
    try:
        if background:
            p = subprocess.Popen(cmd, cwd=cwd)
        else:
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, cwd=cwd)
            res = p.communicate()
    except:
        pass
    return res


def download_file(url):
    ret = subprocess.run(["curl", "-OL", url])
    return not ret.returncode


class VM:
    def __str__(self):
        return self.__class__.__name__

    def __init__(self, username, password, disk_image=None, num=0, ram=4096):
        self.logger = logging.getLogger()

        # username / password to configure
        self.username = username
        self.password = password

        self.num = num
        self.image = disk_image

        self.running = False
        self.spins = 0
        self.p = None
        self.tn = None

        #  various settings
        self.uuid = None
        self.fake_start_date = None
        self.nic_type = "e1000"
        self.num_nics = 0
        self.nics_per_pci_bus = 26 # tested to work with XRv
        self.smbios = []
        overlay_disk_image = re.sub(r'(\.[^.]+$)', r'-overlay\1', disk_image)

        if not os.path.exists(overlay_disk_image):
            self.logger.debug("Creating overlay disk image")
            run_command(["qemu-img", "create", "-f", "qcow2", "-b", disk_image, overlay_disk_image])

        self.qemu_args = ["qemu-system-x86_64", "-display", "none", "-machine", "pc" ]
        self.qemu_args.extend(["-monitor", "tcp:0.0.0.0:40%02d,server,nowait" % self.num])
        self.qemu_args.extend(["-m", str(ram),
                               "-serial", "telnet:0.0.0.0:50%02d,server,nowait" % self.num,
                               "-drive", "if=ide,file=%s" % overlay_disk_image])
        # enable hardware assist if KVM is available
        if os.path.exists("/dev/kvm"):
            self.qemu_args.insert(1, '-enable-kvm')

    def start(self):
        self.logger.info("Starting %s" % self.__class__.__name__)
        self.start_time = datetime.datetime.now()

        cmd = list(self.qemu_args)

        # uuid
        if self.uuid:
            cmd.extend(["-uuid", self.uuid])

        # do we have a fake start date?
        if self.fake_start_date:
            cmd.extend(["-rtc", "base=" + self.fake_start_date])

        # smbios
        for e in self.smbios:
            cmd.extend(["-smbios", e])

        # setup PCI buses
        for i in range(1, math.ceil(self.num_nics / self.nics_per_pci_bus) + 1):
            cmd.extend(["-device", "pci-bridge,chassis_nr={},id=pci.{}".format(i, i)])

        # generate mgmt NICs
        cmd.extend(self.gen_mgmt())
        # generate normal NICs
        cmd.extend(self.gen_nics())

        self.logger.debug(cmd)

        self.p = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE, universal_newlines=True)

        try:
            outs, errs = self.p.communicate(timeout=2)
            self.logger.info("STDOUT: %s" % outs)
            self.logger.info("STDERR: %s" % errs)
        except:
            pass

        for i in range(1, MAX_RETRIES+1):
            try:
                self.qm = telnetlib.Telnet("127.0.0.1", 4000 + self.num)
                break
            except:
                self.logger.info("Unable to connect to qemu monitor (port {}), retrying in a second (attempt {})".format(4000 + self.num, i))
                time.sleep(1)
            if i == MAX_RETRIES:
                raise QemuBroken("Unable to connect to qemu monitor on port {}".format(4000 + self.num))

        for i in range(1, MAX_RETRIES+1):
            try:
                self.tn = telnetlib.Telnet("127.0.0.1", 5000 + self.num)
                break
            except:
                self.logger.info("Unable to connect to qemu monitor (port {}), retrying in a second (attempt {})".format(5000 + self.num, i))
                time.sleep(1)
            if i == MAX_RETRIES:
                raise QemuBroken("Unable to connect to qemu monitor on port {}".format(5000 + self.num))
        try:
            outs, errs = self.p.communicate(timeout=2)
            self.logger.info("STDOUT: %s" % outs)
            self.logger.info("STDERR: %s" % errs)
        except:
            pass

    def gen_mgmt(self):
        """ Generate qemu args for the mgmt interface(s)
        """
        res = []
        # mgmt interface is special - we use qemu user mode network
        res.append("-device")
        # vEOS-lab requires its Ma1 interface to be the first in the bus, so let's hardcode it
        if 'vEOS-lab' in self.image:
            res.append(self.nic_type + ",netdev=p%(i)02d,mac=%(mac)s,bus=pci.1,addr=0x2"
                       % { 'i': 0, 'mac': gen_mac(0) })
        else:
            res.append(self.nic_type + ",netdev=p%(i)02d,mac=%(mac)s"
                       % { 'i': 0, 'mac': gen_mac(0) })
        res.append("-netdev")
        res.append("user,id=p%(i)02d,net=10.0.0.0/24,tftp=/tftpboot,hostfwd=tcp::2022-10.0.0.15:22,hostfwd=udp::2161-10.0.0.15:161,hostfwd=tcp::2830-10.0.0.15:830,hostfwd=tcp::2080-10.0.0.15:80,hostfwd=tcp::2443-10.0.0.15:443" % { 'i': 0 })

        return res

    def gen_nics(self):
        """ Generate qemu args for the normal traffic carrying interface(s)
        """
        res = []
        # vEOS-lab requires its Ma1 interface to be the first in the bus, so start normal nics at 2
        if 'vEOS-lab' in self.image:
            range_start = 2
        else:
            range_start = 1
        for i in range(range_start, self.num_nics+1):
            # calc which PCI bus we are on and the local add on that PCI bus
            pci_bus = math.floor(i/self.nics_per_pci_bus) + 1
            addr = (i % self.nics_per_pci_bus) + 1

            res.append("-device")
            res.append("%(nic_type)s,netdev=p%(i)02d,mac=%(mac)s,bus=pci.%(pci_bus)s,addr=0x%(addr)x" % {
                       'nic_type': self.nic_type,
                       'i': i,
                       'pci_bus': pci_bus,
                       'addr': addr,
                       'mac': gen_mac(i)
                    })
            res.append("-netdev")
            res.append("socket,id=p%(i)02d,listen=:100%(i)02d"
                       % { 'i': i })
        return res

    def stop(self):
        """ Stop this VM
        """
        self.running = False

        try:
            self.p.terminate()
        except ProcessLookupError:
            return

        try:
            self.p.communicate(timeout=10)
        except:
            try:
                # this construct is included as an example at
                # https://docs.python.org/3.6/library/subprocess.html but has
                # failed on me so wrapping in another try block. It was this
                # communicate() that failed with:
                # ValueError: Invalid file object: <_io.TextIOWrapper name=3 encoding='ANSI_X3.4-1968'>
                self.p.kill()
                self.p.communicate(timeout=10)
            except:
                # just assume it's dead or will die?
                self.p.wait(timeout=10)

    def restart(self):
        """ Restart this VM
        """
        self.stop()
        self.start()

    def wait_write(self, cmd, wait='#', con=None):
        """ Wait for something on the serial port and then send command

            Defaults to using self.tn as connection but this can be overridden
            by passing a telnetlib.Telnet object in the con argument.
        """
        con_name = 'custom con'
        if con is None:
            con = self.tn

        if con == self.tn:
            con_name = 'serial console'
        if con == self.qm:
            con_name = 'qemu monitor'

        if wait:
            self.logger.trace("waiting for '%s' on %s" % (wait, con_name))
            res = con.read_until(wait.encode())
            self.logger.trace("read from %s: %s" % (con_name, res.decode()))
        self.logger.debug("writing to %s: %s" % (con_name, cmd))
        con.write("{}\r".format(cmd).encode())

    def work(self):
        self.check_qemu()
        if not self.running:
            try:
                self.bootstrap_spin()
            except EOFError:
                self.logger.error("Telnet session was disconncted, restarting")
                self.restart()

    def check_qemu(self):
        """ Check health of qemu. This is mostly just seeing if there's error
            output on STDOUT from qemu which means we restart it.
        """
        if self.p is None:
            self.logger.debug("VM not started; starting!")
            self.start()

        # check for output
        try:
            outs, errs = self.p.communicate(timeout=1)
        except subprocess.TimeoutExpired:
            return
        self.logger.info("STDOUT: %s" % outs)
        self.logger.info("STDERR: %s" % errs)

        if errs != "":
            self.logger.debug("KVM error, restarting")
            self.stop()
            self.start()


class VR:
    def __init__(self, username, password):
        self.logger = logging.getLogger()

        try:
            os.mkdir("/tftpboot")
        except:
            pass

    def update_health(self, exit_status, message):
        health_file = open("/health", "w")
        health_file.write("%d %s" % (exit_status, message))
        health_file.close()

    def start(self):
        """ Start the virtual router
        """
        self.logger.debug("VMs: %s" % self.vms)

        started = False
        while True:
            all_running = True
            for vm in self.vms:
                vm.work()
                if vm.running is not True:
                    all_running = False

            if all_running:
                self.update_health(0, "running")
                started = True
            else:
                if started:
                    self.update_health(1, "VM failed - restarting")
                else:
                    self.update_health(1, "starting")


class DownloadFailed(Exception):
    """The download failed
    """

class QemuBroken(Exception):
    """ Our Qemu instance is somehow broken
    """


class ASA_vm(VM):
    def __init__(self, username, password):
        if not download_file("http://icn-ucs-2.cisco.com/asav992.qcow2"):
            raise DownloadFailed("Unable to download ASA image")

        for e in os.listdir("/"):
            if re.search(".qcow2", e):
                disk_image = "/" + e
        super(ASA_vm, self).__init__(username, password, disk_image=disk_image, ram=2048)
        self.qemu_args.extend(["-boot", "c",
                               "-serial", "telnet:0.0.0.0:50%02d,server,nowait" % (self.num + 1),])
        self.asa_ready = False
        self.bridge_inside = "bridge-inside"
        self.bridge_outside = "bridge-outside"
        self.veth_inside = "eth1"
        self.veth_outside = "eth2"
        self.tap_inside = "tap1"
        self.tap_outside = "tap2"
        self.tap_mgmt = "tap0"

    def gen_mgmt(self):
        """ Generate qemu args for the mgmt interface(s)
        """
        res = []
        # mgmt interface
        res.extend(["-device", "e1000,netdev=network0,mac=%s" % gen_mac(0)])
        res.extend(["-netdev", "tap,id=network0,ifname=tap0,script=no,downscript=no"])
        # inside ASA interface
        res.extend(["-device", "e1000,netdev=network1,mac=%s" % gen_mac(0),
                    "-netdev", "tap,id=network1,ifname=tap1,script=no,downscript=no"])
        # outside ASA interface
        res.extend(["-device", "e1000,netdev=network2,mac=%s" % gen_mac(0),
                    "-netdev", "tap,id=network2,ifname=tap2,script=no,downscript=no"])

        return res

    def bootstrap_spin(self):
        """Boostrap device
        """

        if self.spins > 300:
            # too many spins with no result ->  give up
            self.stop()
            self.start()
            return

        self.logger.debug("Starting telnet interaction with VM")
        (ridx, match, res) = self.tn.expect([b"asa>",
                                             b"Password:",
                                             b"asa#"], 1)
        if match:  # got a match!
            if ridx == 0:  # asa> prompt
                self.asa_ready = True

                # Set startup configuration
                if not self.bootstrap_config():
                    # main config failed :/
                    self.logger.debug('bootstrap_config failed, restarting device')
                    self.stop()
                    self.start()
                    return

                # close telnet connection
                self.tn.close()

                # Connect interfaces
                self.connect()

                # startup time?
                startup_time = datetime.datetime.now() - self.start_time
                self.logger.info("Startup complete in: %s" % startup_time)

                # mark as running
                self.running = True
                return

        # no match, if we saw some output from the router it's probably
        # booting, so let's give it some more time
        if res != b'':
            self.logger.trace("OUTPUT: %s" % res.decode())
            # reset spins if we saw some output
            self.spins = 0

        self.spins += 1

        return

    def connect(self):
        """
        Connect ASA to neighbors, using pyroute
        """
        ipr = IPRoute()

        self.logger.info("Connecting ASA vm to external veth interfaces")

        # Create inside bridge
        ipr.link('add', ifname=self.bridge_inside, kind='bridge')
        # Lookup the index
        dev_br_inside = ipr.link_lookup(ifname=self.bridge_inside)[0]
        # Bring it down
        ipr.link('set', index=dev_br_inside, state='down')

        # Create outside bridge
        ipr.link('add', ifname=self.bridge_outside, kind='bridge')
        # Lookup the index
        dev_br_outside = ipr.link_lookup(ifname=self.bridge_outside)[0]
        # Bring it down
        ipr.link('set', index=dev_br_outside, state='down')

        # Add inside tap and inside veth to bridge
        dev_tap_inside = ipr.link_lookup(ifname=self.tap_inside)[0]
        ipr.link('set', index=dev_tap_inside, master=dev_br_inside)

        dev_veth_inside = ipr.link_lookup(ifname=self.veth_inside)[0]
        ipr.flush_addr(index=dev_veth_inside)
        ipr.link('set', index=dev_veth_inside, master=dev_br_inside)

        # Add outside tap and outside veth to outside bridge
        dev_tap_outside = ipr.link_lookup(ifname=self.tap_outside)[0]
        ipr.link('set', index=dev_tap_outside, master=dev_br_outside)

        dev_veth_outside = ipr.link_lookup(ifname=self.veth_outside)[0]
        ipr.flush_addr(index=dev_veth_outside)
        ipr.link('set', index=dev_veth_outside, master=dev_br_outside)

        # Bring interfaces up
        ipr.link('set', index=dev_br_inside, state='up')
        ipr.link('set', index=dev_br_outside, state='up')
        ipr.link('set', index=dev_tap_inside, state='up')
        ipr.link('set', index=dev_tap_outside, state='up')
        ipr.link('set', index=dev_veth_inside, state='up')
        ipr.link('set', index=dev_veth_outside, state='up')

        dev_tap_mgmt = ipr.link_lookup(ifname=self.tap_mgmt)[0]
        ipr.link('set', index=dev_tap_mgmt, state='up')
        ipr.addr('add', index=dev_tap_mgmt,
                 address='10.69.0.2', mask=24, broadcast='10.69.0.255')

    def bootstrap_config(self):
        """ Do the actual bootstrap config
        """
        self.logger.info("applying bootstrap configuration")

        # Enter the configuration mode
        self.wait_write("enable", wait=None)
        self.wait_write("", wait="Password:")
        self.wait_write("conf t")
        
        with open("./asa.conf", 'r') as config_file:
            for line in config_file:
                line = line.strip()
                if line and not line.startswith('#'):
                    self.wait_write(line)

        return True

    def _wait_config(self, show_cmd, expect):
        """ Some configuration takes some time to "show up".
            To make sure the device is really ready, wait here.
        """
        self.logger.debug('waiting for {} to appear in {}'.format(expect, show_cmd))
        wait_spins = 0
        # 10s * 90 = 900s = 15min timeout
        while wait_spins < 90:
            self.wait_write(show_cmd, wait=None)
            _, match, data = self.tn.expect([expect.encode('UTF-8')], timeout=10)
            self.logger.trace(data.decode('UTF-8'))
            if match:
                self.logger.debug('a wild {} has appeared!'.format(expect))
                return True
            wait_spins += 1
        self.logger.error('{} not found in {}'.format(expect, show_cmd))
        return False


class ASA(VR):
    def __init__(self, username, password):
        super(ASA, self).__init__(username, password)
        self.vms = [ASA_vm(username, password)]



if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='')
    parser.add_argument('--trace', action='store_true', help='enable trace level logging')
    parser.add_argument('--username', default='admin', help='Username')
    parser.add_argument('--password', default='cisco', help='Password')
#    parser.add_argument('--nics', type=int, default=128, help='Number of NICS')
    args = parser.parse_args()

    LOG_FORMAT = "%(asctime)s: %(module)-10s %(levelname)-8s %(message)s"
    logging.basicConfig(format=LOG_FORMAT)
    logger = logging.getLogger()

    logger.setLevel(logging.DEBUG)
    if args.trace:
        logger.setLevel(1)

    asa = ASA(args.username, args.password)
    asa.start()
