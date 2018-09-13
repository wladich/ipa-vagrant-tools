#!/usr/bin/python3
# Author: Martin Basti
# See LICENSE file for license

import sys
import os
import io
import shutil
import logging
import subprocess
import select
import paramiko  # python3-paramiko

from . import constants
from .config import IPAVagrantConfig, IPATopoConfig
from .vagrant import VagrantFile, VagrantCtl


class IPACITopology(VagrantCtl):
    """Class for operations with IPA CI topologies in Vagrant
    """

    def __init__(
            self, path, config=None,
            replicas=0, clients=0):
        super(IPACITopology, self).__init__(path)
        assert config is None or isinstance(config, IPAVagrantConfig)
        self.config = config

        if not self.config:
            self.config = IPAVagrantConfig()

        self.vagrant_file = VagrantFile(
            self.path, self.config, replicas, clients)

    def _create_directories(self):
        logging.debug("Creating directory structure for '%s' topology",
                      os.path.basename(self.path))
        os.mkdir(self.path)
        os.mkdir(os.path.join(self.path, constants.RPMS_DIR))
        os.mkdir(os.path.join(self.path, constants.PROVISIONING_DIR))

    def create(self):
        logging.info("Preparing '%s' topology",
                     os.path.basename(self.path))

        self._create_directories()

        # generate SSH keys for controller
        command = [
            "ssh-keygen",
            "-f", str(os.path.join(self.path, constants.CONTROLLER_SSH_KEY)),
            "-m", "PEM",
            "-P", "",
        ]
        logging.debug("Generate SSH keys for '%s' topology",
                      os.path.basename(self.path))
        proc = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE)
        try:
            outs, errs = proc.communicate(timeout=15)
            logging.debug("Keygen stdout: %s\n",
                          outs.decode(sys.stdout.encoding))
            logging.debug("Keygen stderr: %s\n",
                          errs.decode(sys.stderr.encoding))
        except subprocess.TimeoutExpired:
            proc.kill()
            raise RuntimeError("Timeout during generating SSH keys")
        else:
            if proc.returncode is not None and proc.returncode != 0:
                raise RuntimeError("Failed to generate SSH key: %s" % errs)

        path = os.path.join(self.path, constants.VAGRANT_FILE)
        with io.open(path, "w") as f:
            f.write(self.vagrant_file.generate_vagrant_file())
            f.close()

        self.vagrant_file.export_ci_config_file(
            os.path.join(self.path, self.config.ci_config_file),
            self.config.ipa_ci_ad_admin_name,
            self.config.ipa_ci_ad_admin_password,
            self.config.ipa_ci_admin_name,
            self.config.ipa_ci_admin_password,
            self.config.ipa_ci_debug,
            self.config.ipa_ci_dirman_dn,
            self.config.ipa_ci_dirman_password,
            self.config.ipa_ci_dns_forwarder,
            self.config.ipa_ci_nis_domain,
            self.config.ipa_ci_ntp_server,
            self.config.ipa_ci_root_ssh_key_filename,
            self.config.ipa_ci_test_dir,
            self.config.ipa_ci_domain_level
        )


class RunTest(object):
    """
    This allows to configure ssh connection to controller machine and start
    test.
    """
    def __init__(self, test_path, ssh_config):

        self.test_path = test_path
        self.controller_hostname = ssh_config['hostname']
        self.controller_username = ssh_config['user']
        self.controller_key_file = ssh_config['identityfile']
        self.controller_port = int(ssh_config['port'])

    def _print_output(self, session, output_stream=None):
        end = False
        while True:
            r, _, _ = select.select([session], [], [], 1.0)
            if session in r:
                while session.recv_ready():
                    data = session.recv(1)
                    sys.stdout.buffer.write(data)  # pylint: disable=no-member
                    if output_stream:
                        output_stream.buffer.write(data)
                sys.stdout.flush()
                while session.recv_stderr_ready():
                    data = session.recv_stderr(1)
                    sys.stderr.buffer.write(data)  # pylint: disable=no-member
                    if output_stream:
                        output_stream.buffer.write(data)
                sys.stderr.flush()
#            if sys.stdin in r:
#                if session.send_ready():
#                    session.send(sys.stdin.read(1))

            if end:
                break

            if session.exit_status_ready():
                end = True  # get all remaining data before break

    def run(self, output_stream=None):

        ssh_client = paramiko.SSHClient()
        ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh_client.connect(
            self.controller_hostname,
            port=self.controller_port,
            username=self.controller_username,
            key_filename=self.controller_key_file)

        try:
            transport = ssh_client.get_transport()

            session = transport.open_channel("session")
            cmd = (
                "sudo "
                "IPATEST_YAML_CONFIG=/vagrant/ipa-test-config.yaml "
                "ipa-run-tests --verbose "
                "{test_path}".format(test_path=self.test_path)
            )
            logging.info("Executing: %s", cmd)
            session.exec_command(cmd)

            self._print_output(session, output_stream)
            logging.info("EXIT STATUS: %s\n", session.recv_exit_status())
        finally:
            ssh_client.close()


class IPACIRunner(object):
    """Class for executing tests
    """
    def __init__(self, tests, config_topo_file=None, config=None,
                 dry_run=False):
        assert isinstance(tests, list)
        assert config is None or isinstance(config, IPAVagrantConfig)
        self.tests = tests
        self.topologies_ready = {}

        self.topo_config = IPATopoConfig(filename=config_topo_file)
        self.config = config
        self.dry_run = dry_run

        # init file is used to store internal information about VM, config,
        # etc..
        self.init_file = os.path.abspath(constants.IPA_RUNNER_INIT_FILE)
        self.rpm_dir = os.path.abspath(constants.RPMS_DIR)

    def create_topology(self, topology_name):
        if topology_name in self.topologies_ready:
            logging.debug("SKIP: Topology '%s' already prepared.",
                          topology_name)
            return self.topologies_ready[topology_name]

        t_config = self.topo_config.topologies.get(topology_name)
        if t_config is None:
            logging.error("topology %s is not specified in config",
                          topology_name)
            raise RuntimeError("Missing topology configuration for {}".format(
                topology_name
            ))

        path = os.path.abspath(topology_name)
        # load all config options that are allowed by DEFAULT_CONFIG
        config_options = {
            key: val for key, val in t_config.items()
            if key in constants.DEFAULT_CONFIG
        }
        config = IPAVagrantConfig(
            filename=t_config.get('config_file'),
            **config_options
        )

        # priority from CLI options and CLI specified config file
        if self.config:
            config.update_config(self.config)

        topo = IPACITopology(
            path,
            config=config,
            replicas=t_config.get('replicas', 0),
            clients=t_config.get('clients', 0),
        )
        self.topologies_ready[topology_name] = topo

        if os.path.exists(path):
            logging.warning("Topology '%s' already exists, skipping topology "
                            "creation", topology_name)
            return topo

        logging.debug("Creating topology %s", topology_name)
        topo.create()
        logging.info("Starting topology %s, this may take long time, please "
                     "wait", topology_name)
        output_file = "vagrant_up_{}.log".format(topology_name)

        # copy custom RPMs to topology
        rpm_files = os.listdir(self.rpm_dir)
        dst = os.path.join(path, constants.RPMS_DIR)
        for fname in rpm_files:
            src = os.path.abspath(os.path.join(self.rpm_dir, fname))
            shutil.copy(src, dst)

        with io.open(output_file, "w") as f:
            # log output to file
            if not self.dry_run:
                topo.up(output_stream=f)  # start VM

        return topo

    def cleanup(self):
        for name, topo in self.topologies_ready.items():
            output_file = "vagrant_destroy_{}.log".format(name)
            logging.info("Cleaning up %s topology", name)
            with io.open(output_file, "w") as f:
                if not self.dry_run:
                    topo.destroy(output_stream=f)
            logging.debug("Removing directory %s", topo.path)
            shutil.rmtree(topo.path)

    def is_initialized(self):
        return os.path.isfile(self.init_file) and os.path.isdir(self.rpm_dir)

    def initialize(self):
        if self.is_initialized():
            raise RuntimeError("IPA CI runner has been already initialized "
                               "in current directory.")

        with io.open(self.init_file, "w") as f:
            f.write("# IPA CI runner init file\n")

        if not os.path.isdir(self.rpm_dir):
            os.mkdir(self.rpm_dir)

    def destroy(self):
        try:
            os.remove(self.init_file)
        except OSError:
            pass

        shutil.rmtree(self.rpm_dir, ignore_errors=True)

    def run(self):
        if not self.is_initialized():
            raise RuntimeError("IPA CI runner must be initialized in current "
                               "directory")

        for test in self.tests:
            if test not in self.topo_config.tests:
                raise RuntimeError("Test {} is not configured".format(test))

            t_config = self.topo_config.tests.get(test)
            if 'path' not in t_config:
                raise RuntimeError(
                    "Test {} doesn't have configured path".format(test))

            test_path = t_config['path']
            topology_name = t_config.get('topology', '_default_')

            topology = self.create_topology(topology_name)

            ssh_config = paramiko.SSHConfig()

            if self.dry_run:
                continue

            ssh_config.parse(io.StringIO(topology.get_ssh_config()))

            r = RunTest(test_path, ssh_config.lookup('controller'))

            output_file = "test_{}.log".format(test)
            with io.open(output_file, "w") as f:
                r.run(output_stream=f)

        self.cleanup()
