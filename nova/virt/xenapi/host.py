# Copyright (c) 2012 Citrix Systems, Inc.
# Copyright 2010 OpenStack Foundation
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""
Management class for host-related functions (start, reboot, etc).
"""

import re

from nova.compute import task_states
from nova.compute import vm_states
from nova import conductor
from nova import context
from nova import exception
from nova.objects import aggregate as aggregate_obj
from nova.objects import instance as instance_obj
from nova.openstack.common.gettextutils import _
from nova.openstack.common import jsonutils
from nova.openstack.common import log as logging
from nova.pci import pci_whitelist
from nova.virt.xenapi import pool_states
from nova.virt.xenapi import vm_utils

LOG = logging.getLogger(__name__)


class Host(object):
    """
    Implements host related operations.
    """
    def __init__(self, session, virtapi):
        self._session = session
        self._virtapi = virtapi
        self._conductor_api = conductor.API()

    def host_power_action(self, _host, action):
        """Reboots or shuts down the host."""
        args = {"action": jsonutils.dumps(action)}
        methods = {"reboot": "host_reboot", "shutdown": "host_shutdown"}
        response = call_xenhost(self._session, methods[action], args)
        return response.get("power_action", response)

    def host_maintenance_mode(self, host, mode):
        """Start/Stop host maintenance window. On start, it triggers
        guest VMs evacuation.
        """
        if not mode:
            return 'off_maintenance'
        host_list = [host_ref for host_ref in
                     self._session.call_xenapi('host.get_all')
                     if host_ref != self._session.host_ref]
        migrations_counter = vm_counter = 0
        ctxt = context.get_admin_context()
        for vm_ref, vm_rec in vm_utils.list_vms(self._session):
            for host_ref in host_list:
                try:
                    # Ensure only guest instances are migrated
                    uuid = vm_rec['other_config'].get('nova_uuid')
                    if not uuid:
                        name = vm_rec['name_label']
                        uuid = _uuid_find(ctxt, host, name)
                        if not uuid:
                            LOG.info(_('Instance %(name)s running on %(host)s'
                                       ' could not be found in the database:'
                                       ' assuming it is a worker VM and skip'
                                       ' ping migration to a new host'),
                                     {'name': name, 'host': host})
                            continue
                    instance = instance_obj.Instance.get_by_uuid(ctxt, uuid)
                    vm_counter = vm_counter + 1

                    aggregate = aggregate_obj.AggregateList.get_by_host(
                        ctxt, host, key=pool_states.POOL_FLAG)
                    if not aggregate:
                        msg = _('Aggregate for host %(host)s count not be'
                                ' found.') % dict(host=host)
                        raise exception.NotFound(msg)

                    dest = _host_find(ctxt, self._session, aggregate[0],
                                      host_ref)
                    instance.host = dest
                    instance.task_state = task_states.MIGRATING
                    instance.save()

                    self._session.call_xenapi('VM.pool_migrate',
                                              vm_ref, host_ref,
                                              {"live": "true"})
                    migrations_counter = migrations_counter + 1

                    instance.vm_state = vm_states.ACTIVE
                    instance.save()

                    break
                except self._session.XenAPI.Failure:
                    LOG.exception(_('Unable to migrate VM %(vm_ref)s '
                                    'from %(host)s'),
                                  {'vm_ref': vm_ref, 'host': host})
                    instance.host = host
                    instance.vm_state = vm_states.ACTIVE
                    instance.save()

        if vm_counter == migrations_counter:
            return 'on_maintenance'
        else:
            raise exception.NoValidHost(reason='Unable to find suitable '
                                                   'host for VMs evacuation')

    def set_host_enabled(self, host, enabled):
        """Sets the specified host's ability to accept new instances."""
        # Since capabilities are gone, use service table to disable a node
        # in scheduler
        status = {'disabled': not enabled,
                'disabled_reason': 'set by xenapi host_state'
                }
        cntxt = context.get_admin_context()
        service = self._conductor_api.service_get_by_args(
                cntxt,
                host,
                'nova-compute')
        self._conductor_api.service_update(
                cntxt,
                service,
                status)

        args = {"enabled": jsonutils.dumps(enabled)}
        response = call_xenhost(self._session, "set_host_enabled", args)
        return response.get("status", response)

    def get_host_uptime(self, _host):
        """Returns the result of calling "uptime" on the target host."""
        response = call_xenhost(self._session, "host_uptime", {})
        return response.get("uptime", response)


class HostState(object):
    """Manages information about the XenServer host this compute
    node is running on.
    """
    def __init__(self, session):
        super(HostState, self).__init__()
        self._session = session
        self._stats = {}
        self._pci_device_filter = pci_whitelist.get_pci_devices_filter()
        self.update_status()

    def _get_passthrough_devices(self):
        """Get a list pci devices that are available for pci passthtough.

        We use a plugin to get the output of the lspci command runs on dom0.
        From this list we will extract pci devices that are using the pciback
        kernel driver. Then we compare this list to the pci whitelist to get
        a new list of pci devices that can be used for pci passthrough.

        :returns: a list of pci devices available for pci passthrough.
        """
        def _compile_hex(pattern):
            """
            Return a compiled regular expression pattern into which we have
            replaced occurrences of hex by [\da-fA-F].
            """
            return re.compile(pattern.replace("hex", r"[\da-fA-F]"))

        def _parse_pci_device_string(dev_string):
            """
            Exctract information from the device string about the slot, the
            vendor and the product ID. The string is as follow:
                "Slot:\tBDF\nClass:\txxxx\nVendor:\txxxx\nDevice:\txxxx\n..."
            Return a dictionary with informations about the device.
            """
            slot_regex = _compile_hex(r"Slot:\t"
                                      r"((?:hex{4}:)?"  # Domain: (optional)
                                      r"hex{2}:"        # Bus:
                                      r"hex{2}\."       # Device.
                                      r"hex{1})")       # Function
            vendor_regex = _compile_hex(r"\nVendor:\t(hex+)")
            product_regex = _compile_hex(r"\nDevice:\t(hex+)")

            slot_id = slot_regex.findall(dev_string)
            vendor_id = vendor_regex.findall(dev_string)
            product_id = product_regex.findall(dev_string)

            if not slot_id or not vendor_id or not product_id:
                raise exception.NovaException(
                    _("Failed to parse information about"
                      " a pci device for passthrough"))

            type_pci = self._session.call_plugin_serialized(
                'xenhost', 'get_pci_type', slot_id[0])

            return {'label': '_'.join(['label',
                                       vendor_id[0],
                                       product_id[0]]),
                    'vendor_id': vendor_id[0],
                    'product_id': product_id[0],
                    'address': slot_id[0],
                    'dev_id': '_'.join(['pci', slot_id[0]]),
                    'dev_type': type_pci,
                    'status': 'available'}

        # Devices are separated by a blank line. That is why we
        # use "\n\n" as separator.
        lspci_out = self._session.call_plugin_serialized(
            'xenhost', 'get_pci_device_details')
        pci_list = lspci_out.split("\n\n")

        # For each device of the list, check if it uses the pciback
        # kernel driver and if it does, get informations and add it
        # to the list of passthrough_devices. Ignore it if the driver
        # is not pciback.
        passthrough_devices = []

        for dev_string_info in pci_list:
            if "Driver:\tpciback" in dev_string_info:
                new_dev = _parse_pci_device_string(dev_string_info)

                if self._pci_device_filter.device_assignable(new_dev):
                    passthrough_devices.append(new_dev)

        return passthrough_devices

    def get_host_stats(self, refresh=False):
        """Return the current state of the host. If 'refresh' is
        True, run the update first.
        """
        if refresh or not self._stats:
            self.update_status()
        return self._stats

    def update_status(self):
        """Since under Xenserver, a compute node runs on a given host,
        we can get host status information using xenapi.
        """
        LOG.debug(_("Updating host stats"))
        data = call_xenhost(self._session, "host_data", {})
        if data:
            sr_ref = vm_utils.scan_default_sr(self._session)
            sr_rec = self._session.call_xenapi("SR.get_record", sr_ref)
            total = int(sr_rec["physical_size"])
            used = int(sr_rec["physical_utilisation"])
            data["disk_total"] = total
            data["disk_used"] = used
            data["disk_available"] = total - used
            data["supported_instances"] = to_supported_instances(
                data.get("host_capabilities")
            )
            host_memory = data.get('host_memory', None)
            if host_memory:
                data["host_memory_total"] = host_memory.get('total', 0)
                data["host_memory_overhead"] = host_memory.get('overhead', 0)
                data["host_memory_free"] = host_memory.get('free', 0)
                data["host_memory_free_computed"] = host_memory.get(
                                                    'free-computed', 0)
                del data['host_memory']
            if (data['host_hostname'] !=
                    self._stats.get('host_hostname', data['host_hostname'])):
                LOG.error(_('Hostname has changed from %(old)s '
                            'to %(new)s. A restart is required to take effect.'
                            ) % {'old': self._stats['host_hostname'],
                                 'new': data['host_hostname']})
                data['host_hostname'] = self._stats['host_hostname']
            data['hypervisor_hostname'] = data['host_hostname']
            vcpus_used = 0
            for vm_ref, vm_rec in vm_utils.list_vms(self._session):
                vcpus_used = vcpus_used + int(vm_rec['VCPUs_max'])
            data['vcpus_used'] = vcpus_used
            data['pci_passthrough_devices'] = self._get_passthrough_devices()
            self._stats = data


def to_supported_instances(host_capabilities):
    if not host_capabilities:
        return []

    result = []
    for capability in host_capabilities:
        try:
            ostype, _version, arch = capability.split("-")
            result.append((arch, 'xapi', ostype))
        except ValueError:
            LOG.warning(
                _("Failed to extract instance support from %s"), capability)

    return result


def call_xenhost(session, method, arg_dict):
    """There will be several methods that will need this general
    handling for interacting with the xenhost plugin, so this abstracts
    out that behavior.
    """
    # Create a task ID as something that won't match any instance ID
    try:
        result = session.call_plugin('xenhost', method, args=arg_dict)
        if not result:
            return ''
        return jsonutils.loads(result)
    except ValueError:
        LOG.exception(_("Unable to get updated status"))
        return None
    except session.XenAPI.Failure as e:
        LOG.error(_("The call to %(method)s returned "
                    "an error: %(e)s."), {'method': method, 'e': e})
        return e.details[1]


def _uuid_find(context, host, name_label):
    """Return instance uuid by name_label."""
    for i in instance_obj.InstanceList.get_by_host(context, host):
        if i.name == name_label:
            return i.uuid
    return None


def _host_find(context, session, src_aggregate, dst):
    """Return the host from the xenapi host reference.

    :param src_aggregate: the aggregate that the compute host being put in
                          maintenance (source of VMs) belongs to
    :param dst: the hypervisor host reference (destination of VMs)

    :return: the compute host that manages dst
    """
    # NOTE: this would be a lot simpler if nova-compute stored
    # CONF.host in the XenServer host's other-config map.
    # TODO(armando-migliaccio): improve according the note above
    uuid = session.call_xenapi('host.get_record', dst)['uuid']
    for compute_host, host_uuid in src_aggregate.metadetails.iteritems():
        if host_uuid == uuid:
            return compute_host
    raise exception.NoValidHost(reason='Host %(host_uuid)s could not be found '
                                'from aggregate metadata: %(metadata)s.' %
                                {'host_uuid': uuid,
                                 'metadata': src_aggregate.metadetails})
