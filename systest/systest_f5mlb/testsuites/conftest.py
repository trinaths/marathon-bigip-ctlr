# Copyright 2017 F5 Networks
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Local pytest plugin."""


import functools
import subprocess

import pytest
from pytest import symbols

import systest_common.src as common

from . import utils


DELETE_TIMEOUT = 2 * 60


def pytest_namespace():
    """Configure objects to go in the pytest namespace."""
    bastion = common.ssh.connect(pytest.symbols.bastion)

    def _run(hosts, *args, **kwargs):
        return [str(bastion.run(host, *args, **kwargs)) for host in hosts]

    return {
        'masters_cmd': functools.partial(_run, pytest.symbols.masters),
        'workers_cmd': functools.partial(_run, pytest.symbols.workers)
    }


@pytest.fixture(scope='session', autouse=True)
def openshift_service_acct(request):
    """Provide a service account with attached to anyuid scc."""
    if symbols.orchestration == "openshift":
        def teardown():
            cmd = ['oc', 'delete', 'serviceaccount',
                   utils.DEFAULT_OPENSHIFT_USER]
            subprocess.check_output(cmd, stderr=subprocess.STDOUT)

        try:
            teardown()
        except subprocess.CalledProcessError:
            pass
        cmd = ['oc', 'create', 'serviceaccount', utils.DEFAULT_OPENSHIFT_USER]
        subprocess.check_output(cmd, stderr=subprocess.STDOUT)

        cmd = ['oc', 'adm', 'policy', 'add-scc-to-user', 'anyuid', '-z',
               utils.DEFAULT_OPENSHIFT_USER]
        subprocess.check_output(cmd, stderr=subprocess.STDOUT)
        request.addfinalizer(teardown)


@pytest.fixture(scope='session', autouse=False)
def orchestration(request):
    """Provide a orchestration connection.

    Attempt to clean all previous objects
    """
    conn = common.orchestration.connect(**vars(symbols))
    conn.namespace = utils.controller_namespace()
    conn.apps.delete(ptn="test-bigip-controller")
    conn.namespace = 'default'

    return conn


@pytest.fixture(scope='session', autouse=True)
def ssh(request):
    """Provide an ssh connection - via the bastion host."""
    return common.ssh.connect(gateway=symbols.bastion)


# Always create an initial bigip
@pytest.fixture(scope='session', autouse=True)
def bigip(request):
    """Provide a bigip connection."""
    return common.bigip.connect(
        utils.DEFAULT_BIGIP_MGMT_IP,
        utils.DEFAULT_BIGIP_USERNAME,
        utils.DEFAULT_BIGIP_PASSWORD
    )


# Only create a second bigip if it is required
@pytest.fixture(scope='session', autouse=False)
def bigip2(request):
    """Provide a bigipX connection."""
    return common.bigip.connect(
        utils.DEFAULT_BIGIP2_MGMT_IP,
        utils.DEFAULT_BIGIP_USERNAME,
        utils.DEFAULT_BIGIP_PASSWORD
    )


def _setup_bigip_network(instance, partition, orchestration):
    if "openshift" == symbols.orchestration:
        # setup the default openshift networking
        profile_full_path = ('/' + partition + '/' +
                             utils.DEFAULT_BIGIP_VXLAN_PROFILE)
        instance.vxlan.create(name=utils.DEFAULT_BIGIP_VXLAN_PROFILE,
                              partition=partition,
                              floodingType='multipoint')
        instance.tunnel.create(name=utils.DEFAULT_BIGIP_VXLAN_TUNNEL,
                               partition=partition,
                               key=0,
                               profile=profile_full_path,
                               localAddress=symbols.bigip_int_ip)
        instance.selfip.create(name=utils.DEFAULT_BIGIP_OPENSHIFT_SELFNAME,
                               partition=partition,
                               address=utils.DEFAULT_BIGIP_OPENSHIFT_SELFIP,
                               vlan=utils.DEFAULT_BIGIP_VXLAN_TUNNEL)

        orchestration.hostsubnets.create(symbols.bigip_name,
                                         symbols.bigip_int_ip,
                                         utils.DEFAULT_BIGIP_OPENSHIFT_SUBNET)


def _setup_bigip(instance, partition, orchestration):
    instance.partition.create(partition, subPath="/")

    # FIXME (kevin): remove these partition hacks when issue #32 is fixed
    p = instance.partition.get(name=partition)
    p.inheritedTrafficGroup = False
    p.trafficGroup = "/Common/traffic-group-local-only"
    p.update()

    _setup_bigip_network(instance, partition, orchestration)


def _teardown_bigip_network(instance, partition, orchestration):
    if "openshift" == symbols.orchestration:
        fdb = instance.fdb_tunnel.get(name=utils.DEFAULT_BIGIP_VXLAN_TUNNEL,
                                      partition=partition)
        if fdb is not None:
            fdb.records = []
            fdb.update()
        instance.selfip.delete(name=utils.DEFAULT_BIGIP_OPENSHIFT_SELFNAME,
                               partition=partition)
        instance.tunnels.delete(partition=partition)
        instance.vxlans.delete(partition=partition)

        if orchestration.hostsubnets.exists(symbols.bigip_name):
            orchestration.hostsubnets.delete(symbols.bigip_name)


def _teardown_bigip(instance, partition, orchestration):
    instance.iapps.delete(partition=partition)
    instance.virtual_servers.delete(partition=partition)
    instance.virtual_addresses.delete(partition=partition)
    instance.pools.delete(partition=partition)
    instance.nodes.delete(partition=partition)
    instance.health_monitors.delete(partition=partition)

    _teardown_bigip_network(instance, partition, orchestration)
    instance.partition.delete(name=partition)


@pytest.fixture(scope='function', autouse=True)
def default_test_fx(request, orchestration, bigip):
    """Default test fixture.

    Create a test partition on test setup.
    Delete all orchestration apps on test teardown.
    Delete test partition on test teardown.
    """
    partition = utils.DEFAULT_F5MLB_PARTITION

    def teardown():
        if request.config._meta.vars.get('skip_teardown', None):
            return
        orchestration.namespace = "default"
        orchestration.apps.delete(timeout=DELETE_TIMEOUT)
        orchestration.deployments.delete(timeout=DELETE_TIMEOUT)
        _teardown_bigip(bigip, partition, orchestration)

    teardown()
    _setup_bigip(bigip, partition, orchestration)
    request.addfinalizer(teardown)


@pytest.fixture(scope='function')
def bigip2_addto_test_fx(request, orchestration, bigip2):
    """Bigip2 test fixture.

    Create a test partition on second bigip.
    Delete all orchestration apps on test teardown.
    Delete test partition on test teardown.
    """
    partition = utils.DEFAULT_F5MLB_PARTITION

    def teardown():
        if request.config._meta.vars.get('skip_teardown', None):
            return
        _teardown_bigip(bigip2, partition, orchestration)

    teardown()
    _setup_bigip(bigip2, partition, orchestration)
    request.addfinalizer(teardown)


@pytest.fixture(scope='function')
def node_controller(request):
    """Provide a node-controller service."""
    node_controller = utils.NodeController()

    def teardown():
        node_controller.run_teardowns()

    request.addfinalizer(teardown)
    return node_controller


@pytest.fixture(scope='function')
def bigip_controller(request, orchestration):
    """Provide a default bigip-controller service."""
    mode = request.config._meta.vars.get(
        'controller-pool-mode', utils.POOL_MODE_CLUSTER)
    assert mode in utils.POOL_MODES, "controller-pool-mode var is invalid"

    controller = utils.BigipController(orchestration, pool_mode=mode).create()

    def teardown():
        if request.config._meta.vars.get('skip_teardown', None):
            return
        orchestration.namespace = utils.controller_namespace()
        controller.delete()

    request.addfinalizer(teardown)
    return controller


@pytest.fixture(scope='function')
def bigip2_controller(request, orchestration, bigip2_addto_test_fx):
    """Provide a second bigip-controller service."""
    mode = request.config._meta.vars.get(
        'controller-pool-mode', utils.POOL_MODE_CLUSTER)
    assert mode in utils.POOL_MODES, "controller-pool-mode var is invalid"
    controller = utils.BigipController(
        orchestration, pool_mode=mode, id=utils.BIGIP2_F5MLB_NAME,
        config=utils.BIGIP2_F5MLB_CONFIG).create()

    def teardown():
        if request.config._meta.vars.get('skip_teardown', None):
            return
        orchestration.namespace = utils.controller_namespace()
        controller.delete()

    request.addfinalizer(teardown)
    return controller


@pytest.fixture(scope='function')
def scale_controller(request, orchestration):
    """Provide a scaling BigIP controller service."""
    controller = utils.deploy_controller(request, orchestration)

    def teardown():
        if request.config._meta.vars.get('skip_teardown', None):
            return
        orchestration.namespace = utils.controller_namespace()
        controller.delete()

    request.addfinalizer(teardown)
    return controller
