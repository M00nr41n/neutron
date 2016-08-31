# Copyright (c) 2012 OpenStack Foundation.
# All Rights Reserved.
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

import functools

import netaddr
from neutron_lib.api import validators
from neutron_lib import constants
from neutron_lib.db import utils as db_utils
from neutron_lib import exceptions as exc
from oslo_config import cfg
from oslo_db import exception as os_db_exc
from oslo_db.sqlalchemy import utils as sa_utils
from oslo_log import log as logging
from oslo_utils import excutils
from oslo_utils import uuidutils
from sqlalchemy import and_
from sqlalchemy import event
from sqlalchemy import not_

from neutron._i18n import _, _LE, _LI
from neutron.api.rpc.agentnotifiers import l3_rpc_agent_api
from neutron.api.v2 import attributes
from neutron.callbacks import events
from neutron.callbacks import exceptions
from neutron.callbacks import registry
from neutron.callbacks import resources
from neutron.common import constants as n_const
from neutron.common import exceptions as n_exc
from neutron.common import ipv6_utils
from neutron.common import utils
from neutron import context as ctx
from neutron.db import _utils as ndb_utils
from neutron.db import api as db_api
from neutron.db import db_base_plugin_common
from neutron.db import ipam_pluggable_backend
from neutron.db import models_v2
from neutron.db import rbac_db_mixin as rbac_mixin
from neutron.db import rbac_db_models as rbac_db
from neutron.db import standardattrdescription_db as stattr_db
from neutron.extensions import ip_allocation as ipa
from neutron.extensions import l3
from neutron import ipam
from neutron.ipam import exceptions as ipam_exc
from neutron.ipam import subnet_alloc
from neutron import manager
from neutron import neutron_plugin_base_v2
from neutron.objects import base as base_obj
from neutron.objects import subnetpool as subnetpool_obj
from neutron.plugins.common import constants as service_constants


LOG = logging.getLogger(__name__)

# Ports with the following 'device_owner' values will not prevent
# network deletion.  If delete_network() finds that all ports on a
# network have these owners, it will explicitly delete each port
# and allow network deletion to continue.  Similarly, if delete_subnet()
# finds out that all existing IP Allocations are associated with ports
# with these owners, it will allow subnet deletion to proceed with the
# IP allocations being cleaned up by cascade.
AUTO_DELETE_PORT_OWNERS = [constants.DEVICE_OWNER_DHCP]

DNS_DOMAIN_DEFAULT = 'openstacklocal.'
FQDN_MAX_LEN = 255


def _check_subnet_not_used(context, subnet_id):
    try:
        kwargs = {'context': context, 'subnet_id': subnet_id}
        registry.notify(
            resources.SUBNET, events.BEFORE_DELETE, None, **kwargs)
    except exceptions.CallbackFailure as e:
        raise exc.SubnetInUse(subnet_id=subnet_id, reason=e)


def _update_subnetpool_dict(orig_pool, new_pool):
    keys_to_update = (
        set(orig_pool.fields.keys()) - set(orig_pool.synthetic_fields))
    updated = {k: new_pool.get(k, orig_pool[k]) for k in keys_to_update}

    new_prefixes = new_pool.get('prefixes', constants.ATTR_NOT_SPECIFIED)
    if new_prefixes is not constants.ATTR_NOT_SPECIFIED:
        orig_ip_set = netaddr.IPSet(orig_pool.prefixes)
        new_ip_set = netaddr.IPSet(new_prefixes)
        if not orig_ip_set.issubset(new_ip_set):
            msg = _("Existing prefixes must be "
                    "a subset of the new prefixes")
            raise n_exc.IllegalSubnetPoolPrefixUpdate(msg=msg)
        new_ip_set.compact()
        updated['prefixes'] = [str(prefix.cidr)
                               for prefix in new_ip_set.iter_cidrs()]
    else:
        updated['prefixes'] = [str(prefix)
                               for prefix in orig_pool.prefixes]
    return updated


class NeutronDbPluginV2(db_base_plugin_common.DbBasePluginCommon,
                        neutron_plugin_base_v2.NeutronPluginBaseV2,
                        rbac_mixin.RbacPluginMixin,
                        stattr_db.StandardAttrDescriptionMixin):
    """V2 Neutron plugin interface implementation using SQLAlchemy models.

    Whenever a non-read call happens the plugin will call an event handler
    class method (e.g., network_created()).  The result is that this class
    can be sub-classed by other classes that add custom behaviors on certain
    events.
    """

    # This attribute specifies whether the plugin supports or not
    # bulk/pagination/sorting operations. Name mangling is used in
    # order to ensure it is qualified by class
    __native_bulk_support = True
    __native_pagination_support = True
    __native_sorting_support = True

    def __init__(self):
        self.set_ipam_backend()
        if cfg.CONF.notify_nova_on_port_status_changes:
            # Import nova conditionally to support the use case of Neutron
            # being used outside of an OpenStack context.
            from neutron.notifiers import nova
            # NOTE(arosen) These event listeners are here to hook into when
            # port status changes and notify nova about their change.
            self.nova_notifier = nova.Notifier.get_instance()
            event.listen(models_v2.Port, 'after_insert',
                         self.nova_notifier.send_port_status)
            event.listen(models_v2.Port, 'after_update',
                         self.nova_notifier.send_port_status)
            event.listen(models_v2.Port.status, 'set',
                         self.nova_notifier.record_port_status_changed)
        for e in (events.BEFORE_CREATE, events.BEFORE_UPDATE,
                  events.BEFORE_DELETE):
            registry.subscribe(self.validate_network_rbac_policy_change,
                               rbac_mixin.RBAC_POLICY, e)

    @db_api.retry_if_session_inactive()
    def validate_network_rbac_policy_change(self, resource, event, trigger,
                                            context, object_type, policy,
                                            **kwargs):
        """Validates network RBAC policy changes.

        On creation, verify that the creator is an admin or that it owns the
        network it is sharing.

        On update and delete, make sure the tenant losing access does not have
        resources that depend on that access.
        """
        if object_type != 'network' or policy['action'] != 'access_as_shared':
            # we only care about shared network policies
            return
        # The object a policy targets cannot be changed so we can look
        # at the original network for the update event as well.
        net = self._get_network(context, policy['object_id'])
        if event in (events.BEFORE_CREATE, events.BEFORE_UPDATE):
            # we still have to verify that the caller owns the network because
            # _get_network will succeed on a shared network
            if not context.is_admin and net['tenant_id'] != context.tenant_id:
                msg = _("Only admins can manipulate policies on networks "
                        "they do not own")
                raise exc.InvalidInput(error_message=msg)

        tenant_to_check = None
        if event == events.BEFORE_UPDATE:
            new_tenant = kwargs['policy_update']['target_tenant']
            if policy['target_tenant'] != new_tenant:
                tenant_to_check = policy['target_tenant']

        if event == events.BEFORE_DELETE:
            tenant_to_check = policy['target_tenant']

        if tenant_to_check:
            self.ensure_no_tenant_ports_on_network(net['id'], net['tenant_id'],
                                                   tenant_to_check)

    def ensure_no_tenant_ports_on_network(self, network_id, net_tenant_id,
                                          tenant_id):
        ctx_admin = ctx.get_admin_context()
        rb_model = rbac_db.NetworkRBAC
        other_rbac_entries = self._model_query(ctx_admin, rb_model).filter(
            and_(rb_model.object_id == network_id,
                 rb_model.action == 'access_as_shared'))
        ports = self._model_query(ctx_admin, models_v2.Port).filter(
            models_v2.Port.network_id == network_id)
        if tenant_id == '*':
            # for the wildcard we need to get all of the rbac entries to
            # see if any allow the remaining ports on the network.
            other_rbac_entries = other_rbac_entries.filter(
                rb_model.target_tenant != tenant_id)
            # any port with another RBAC entry covering it or one belonging to
            # the same tenant as the network owner is ok
            allowed_tenants = [entry['target_tenant']
                               for entry in other_rbac_entries]
            allowed_tenants.append(net_tenant_id)
            ports = ports.filter(
                ~models_v2.Port.tenant_id.in_(allowed_tenants))
        else:
            # if there is a wildcard rule, we can return early because it
            # allows any ports
            query = other_rbac_entries.filter(rb_model.target_tenant == '*')
            if query.count():
                return
            ports = ports.filter(models_v2.Port.tenant_id == tenant_id)
        if ports.count():
            raise n_exc.InvalidSharedSetting(network=network_id)

    def set_ipam_backend(self):
        self.ipam = ipam_pluggable_backend.IpamPluggableBackend()

    def _validate_host_route(self, route, ip_version):
        try:
            netaddr.IPNetwork(route['destination'])
            netaddr.IPAddress(route['nexthop'])
        except netaddr.core.AddrFormatError:
            err_msg = _("Invalid route: %s") % route
            raise exc.InvalidInput(error_message=err_msg)
        except ValueError:
            # netaddr.IPAddress would raise this
            err_msg = _("Invalid route: %s") % route
            raise exc.InvalidInput(error_message=err_msg)
        self._validate_ip_version(ip_version, route['nexthop'], 'nexthop')
        self._validate_ip_version(ip_version, route['destination'],
                                  'destination')

    def _validate_shared_update(self, context, id, original, updated):
        # The only case that needs to be validated is when 'shared'
        # goes from True to False
        if updated['shared'] == original.shared or updated['shared']:
            return
        ports = self._model_query(
            context, models_v2.Port).filter(models_v2.Port.network_id == id)
        ports = ports.filter(not_(models_v2.Port.device_owner.startswith(
            constants.DEVICE_OWNER_NETWORK_PREFIX)))
        subnets = self._model_query(
            context, models_v2.Subnet).filter(
                models_v2.Subnet.network_id == id)
        tenant_ids = set([port['tenant_id'] for port in ports] +
                         [subnet['tenant_id'] for subnet in subnets])
        # raise if multiple tenants found or if the only tenant found
        # is not the owner of the network
        if (len(tenant_ids) > 1 or len(tenant_ids) == 1 and
            tenant_ids.pop() != original.tenant_id):
            raise n_exc.InvalidSharedSetting(network=original.name)

    def _validate_ipv6_attributes(self, subnet, cur_subnet):
        if cur_subnet:
            self._validate_ipv6_update_dhcp(subnet, cur_subnet)
            return
        ra_mode_set = validators.is_attr_set(subnet.get('ipv6_ra_mode'))
        address_mode_set = validators.is_attr_set(
            subnet.get('ipv6_address_mode'))
        self._validate_ipv6_dhcp(ra_mode_set, address_mode_set,
                                 subnet['enable_dhcp'])
        if ra_mode_set and address_mode_set:
            self._validate_ipv6_combination(subnet['ipv6_ra_mode'],
                                            subnet['ipv6_address_mode'])
        if address_mode_set or ra_mode_set:
            self._validate_eui64_applicable(subnet)

    def _validate_eui64_applicable(self, subnet):
        # Per RFC 4862, section 5.5.3, prefix length and interface
        # id together should be equal to 128. Currently neutron supports
        # EUI64 interface id only, thus limiting the prefix
        # length to be 64 only.
        if ipv6_utils.is_auto_address_subnet(subnet):
            if netaddr.IPNetwork(subnet['cidr']).prefixlen != 64:
                msg = _('Invalid CIDR %s for IPv6 address mode. '
                        'OpenStack uses the EUI-64 address format, '
                        'which requires the prefix to be /64')
                raise exc.InvalidInput(
                    error_message=(msg % subnet['cidr']))

    def _validate_ipv6_combination(self, ra_mode, address_mode):
        if ra_mode != address_mode:
            msg = _("ipv6_ra_mode set to '%(ra_mode)s' with ipv6_address_mode "
                    "set to '%(addr_mode)s' is not valid. "
                    "If both attributes are set, they must be the same value"
                    ) % {'ra_mode': ra_mode, 'addr_mode': address_mode}
            raise exc.InvalidInput(error_message=msg)

    def _validate_ipv6_dhcp(self, ra_mode_set, address_mode_set, enable_dhcp):
        if (ra_mode_set or address_mode_set) and not enable_dhcp:
            msg = _("ipv6_ra_mode or ipv6_address_mode cannot be set when "
                    "enable_dhcp is set to False")
            raise exc.InvalidInput(error_message=msg)

    def _validate_ipv6_update_dhcp(self, subnet, cur_subnet):
        if ('enable_dhcp' in subnet and not subnet['enable_dhcp']):
            msg = _("Cannot disable enable_dhcp with "
                    "ipv6 attributes set")

            ra_mode_set = validators.is_attr_set(subnet.get('ipv6_ra_mode'))
            address_mode_set = validators.is_attr_set(
                subnet.get('ipv6_address_mode'))

            if ra_mode_set or address_mode_set:
                raise exc.InvalidInput(error_message=msg)

            old_ra_mode_set = validators.is_attr_set(
                cur_subnet.get('ipv6_ra_mode'))
            old_address_mode_set = validators.is_attr_set(
                cur_subnet.get('ipv6_address_mode'))

            if old_ra_mode_set or old_address_mode_set:
                raise exc.InvalidInput(error_message=msg)

    def _create_bulk(self, resource, context, request_items):
        objects = []
        collection = "%ss" % resource
        items = request_items[collection]
        context.session.begin(subtransactions=True)
        try:
            for item in items:
                obj_creator = getattr(self, 'create_%s' % resource)
                objects.append(obj_creator(context, item))
            context.session.commit()
        except Exception:
            context.session.rollback()
            with excutils.save_and_reraise_exception():
                LOG.error(_LE("An exception occurred while creating "
                              "the %(resource)s:%(item)s"),
                          {'resource': resource, 'item': item})
        return objects

    @db_api.retry_if_session_inactive()
    def create_network_bulk(self, context, networks):
        return self._create_bulk('network', context, networks)

    @db_api.retry_if_session_inactive()
    def create_network(self, context, network):
        """Handle creation of a single network."""
        net_db = self.create_network_db(context, network)
        return self._make_network_dict(net_db, process_extensions=False,
                                       context=context)

    def create_network_db(self, context, network):
        # single request processing
        n = network['network']
        # NOTE(jkoelker) Get the tenant_id outside of the session to avoid
        #                unneeded db action if the operation raises
        tenant_id = n['tenant_id']
        with context.session.begin(subtransactions=True):
            args = {'tenant_id': tenant_id,
                    'id': n.get('id') or uuidutils.generate_uuid(),
                    'name': n['name'],
                    'admin_state_up': n['admin_state_up'],
                    'status': n.get('status', constants.NET_STATUS_ACTIVE),
                    'description': n.get('description')}
            network = models_v2.Network(**args)
            if n['shared']:
                entry = rbac_db.NetworkRBAC(
                    network=network, action='access_as_shared',
                    target_tenant='*', tenant_id=network['tenant_id'])
                context.session.add(entry)
            context.session.add(network)
        return network

    @db_api.retry_if_session_inactive()
    def update_network(self, context, id, network):
        n = network['network']
        with context.session.begin(subtransactions=True):
            network = self._get_network(context, id)
            # validate 'shared' parameter
            if 'shared' in n:
                entry = None
                for item in network.rbac_entries:
                    if (item.action == 'access_as_shared' and
                            item.target_tenant == '*'):
                        entry = item
                        break
                setattr(network, 'shared', True if entry else False)
                self._validate_shared_update(context, id, network, n)
                update_shared = n.pop('shared')
                if update_shared and not entry:
                    entry = rbac_db.NetworkRBAC(
                        network=network, action='access_as_shared',
                        target_tenant='*', tenant_id=network['tenant_id'])
                    context.session.add(entry)
                elif not update_shared and entry:
                    context.session.delete(entry)
                    context.session.expire(network, ['rbac_entries'])
            # The filter call removes attributes from the body received from
            # the API that are logically tied to network resources but are
            # stored in other database tables handled by extensions
            network.update(
                ndb_utils.filter_non_model_columns(n, models_v2.Network))
        return self._make_network_dict(network, context=context)

    @db_api.retry_if_session_inactive()
    def delete_network(self, context, id):
        with context.session.begin(subtransactions=True):
            network = self._get_network(context, id)

            context.session.query(models_v2.Port).filter_by(
                network_id=id).filter(
                models_v2.Port.device_owner.
                in_(AUTO_DELETE_PORT_OWNERS)).delete(synchronize_session=False)

            port_in_use = context.session.query(models_v2.Port).filter_by(
                network_id=id).first()

            if port_in_use:
                raise exc.NetworkInUse(net_id=id)

            # clean up subnets
            subnets = self._get_subnets_by_network(context, id)
            for subnet in subnets:
                self.delete_subnet(context, subnet['id'])

            context.session.delete(network)

    @db_api.retry_if_session_inactive()
    def get_network(self, context, id, fields=None):
        network = self._get_network(context, id)
        return self._make_network_dict(network, fields, context=context)

    @db_api.retry_if_session_inactive()
    def get_networks(self, context, filters=None, fields=None,
                     sorts=None, limit=None, marker=None,
                     page_reverse=False):
        marker_obj = self._get_marker_obj(context, 'network', limit, marker)
        make_network_dict = functools.partial(self._make_network_dict,
                                              context=context)
        return self._get_collection(context, models_v2.Network,
                                    make_network_dict,
                                    filters=filters, fields=fields,
                                    sorts=sorts,
                                    limit=limit,
                                    marker_obj=marker_obj,
                                    page_reverse=page_reverse)

    @db_api.retry_if_session_inactive()
    def get_networks_count(self, context, filters=None):
        return self._get_collection_count(context, models_v2.Network,
                                          filters=filters)

    @db_api.retry_if_session_inactive()
    def create_subnet_bulk(self, context, subnets):
        return self._create_bulk('subnet', context, subnets)

    def _validate_ip_version(self, ip_version, addr, name):
        """Check IP field of a subnet match specified ip version."""
        ip = netaddr.IPNetwork(addr)
        if ip.version != ip_version:
            data = {'name': name,
                    'addr': addr,
                    'ip_version': ip_version}
            msg = _("%(name)s '%(addr)s' does not match "
                    "the ip_version '%(ip_version)s'") % data
            raise exc.InvalidInput(error_message=msg)

    def _validate_subnet(self, context, s, cur_subnet=None):
        """Validate a subnet spec."""

        # This method will validate attributes which may change during
        # create_subnet() and update_subnet().
        # The method requires the subnet spec 's' has 'ip_version' field.
        # If 's' dict does not have 'ip_version' field in an API call
        # (e.g., update_subnet()), you need to set 'ip_version' field
        # before calling this method.

        ip_ver = s['ip_version']

        if validators.is_attr_set(s.get('cidr')):
            self._validate_ip_version(ip_ver, s['cidr'], 'cidr')

        # TODO(watanabe.isao): After we found a way to avoid the re-sync
        # from the agent side, this restriction could be removed.
        if cur_subnet:
            dhcp_was_enabled = cur_subnet.enable_dhcp
        else:
            dhcp_was_enabled = False
        if s.get('enable_dhcp') and not dhcp_was_enabled:
            subnet_prefixlen = netaddr.IPNetwork(s['cidr']).prefixlen
            error_message = _("Subnet has a prefix length that is "
                              "incompatible with DHCP service enabled")
            if ((ip_ver == 4 and subnet_prefixlen > 30) or
                (ip_ver == 6 and subnet_prefixlen > 126)):
                raise exc.InvalidInput(error_message=error_message)

            net = netaddr.IPNetwork(s['cidr'])
            if net.is_multicast():
                error_message = _("Multicast IP subnet is not supported "
                                  "if enable_dhcp is True")
                raise exc.InvalidInput(error_message=error_message)
            elif net.is_loopback():
                error_message = _("Loopback IP subnet is not supported "
                                  "if enable_dhcp is True")
                raise exc.InvalidInput(error_message=error_message)

        if validators.is_attr_set(s.get('gateway_ip')):
            self._validate_ip_version(ip_ver, s['gateway_ip'], 'gateway_ip')
            is_gateway_not_valid = (
                ipam.utils.check_gateway_invalid_in_subnet(
                    s['cidr'], s['gateway_ip']))
            if is_gateway_not_valid:
                error_message = _("Gateway is not valid on subnet")
                raise exc.InvalidInput(error_message=error_message)
            # Ensure the gateway IP is not assigned to any port
            # skip this check in case of create (s parameter won't have id)
            # NOTE(salv-orlando): There is slight chance of a race, when
            # a subnet-update and a router-interface-add operation are
            # executed concurrently
            if cur_subnet and not ipv6_utils.is_ipv6_pd_enabled(s):
                ipal = models_v2.IPAllocation
                alloc_qry = context.session.query(ipal)
                alloc_qry = alloc_qry.join("port", "routerport")
                allocated = alloc_qry.filter(
                    ipal.ip_address == cur_subnet['gateway_ip'],
                    ipal.subnet_id == cur_subnet['id']).first()
                if allocated and allocated['port_id']:
                    raise n_exc.GatewayIpInUse(
                        ip_address=cur_subnet['gateway_ip'],
                        port_id=allocated['port_id'])

        if validators.is_attr_set(s.get('dns_nameservers')):
            if len(s['dns_nameservers']) > cfg.CONF.max_dns_nameservers:
                raise n_exc.DNSNameServersExhausted(
                    subnet_id=s.get('id', _('new subnet')),
                    quota=cfg.CONF.max_dns_nameservers)
            for dns in s['dns_nameservers']:
                try:
                    netaddr.IPAddress(dns)
                except Exception:
                    raise exc.InvalidInput(
                        error_message=(_("Error parsing dns address %s") %
                                       dns))
                self._validate_ip_version(ip_ver, dns, 'dns_nameserver')

        if validators.is_attr_set(s.get('host_routes')):
            if len(s['host_routes']) > cfg.CONF.max_subnet_host_routes:
                raise n_exc.HostRoutesExhausted(
                    subnet_id=s.get('id', _('new subnet')),
                    quota=cfg.CONF.max_subnet_host_routes)
            # check if the routes are all valid
            for rt in s['host_routes']:
                self._validate_host_route(rt, ip_ver)

        if ip_ver == 4:
            if validators.is_attr_set(s.get('ipv6_ra_mode')):
                raise exc.InvalidInput(
                    error_message=(_("ipv6_ra_mode is not valid when "
                                     "ip_version is 4")))
            if validators.is_attr_set(s.get('ipv6_address_mode')):
                raise exc.InvalidInput(
                    error_message=(_("ipv6_address_mode is not valid when "
                                     "ip_version is 4")))
        if ip_ver == 6:
            self._validate_ipv6_attributes(s, cur_subnet)

    def _validate_subnet_for_pd(self, subnet):
        """Validates that subnet parameters are correct for IPv6 PD"""
        if (subnet.get('ip_version') != constants.IP_VERSION_6):
            reason = _("Prefix Delegation can only be used with IPv6 "
                       "subnets.")
            raise exc.BadRequest(resource='subnets', msg=reason)

        mode_list = [constants.IPV6_SLAAC,
                     constants.DHCPV6_STATELESS]

        ra_mode = subnet.get('ipv6_ra_mode')
        if ra_mode not in mode_list:
            reason = _("IPv6 RA Mode must be SLAAC or Stateless for "
                       "Prefix Delegation.")
            raise exc.BadRequest(resource='subnets', msg=reason)

        address_mode = subnet.get('ipv6_address_mode')
        if address_mode not in mode_list:
            reason = _("IPv6 Address Mode must be SLAAC or Stateless for "
                       "Prefix Delegation.")
            raise exc.BadRequest(resource='subnets', msg=reason)

    def _update_router_gw_ports(self, context, network, subnet):
        l3plugin = manager.NeutronManager.get_service_plugins().get(
                service_constants.L3_ROUTER_NAT)
        if l3plugin:
            gw_ports = self._get_router_gw_ports_by_network(context,
                    network['id'])
            router_ids = [p['device_id'] for p in gw_ports]
            ctx_admin = context.elevated()
            ext_subnets_dict = {s['id']: s for s in network['subnets']}
            for id in router_ids:
                router = l3plugin.get_router(ctx_admin, id)
                external_gateway_info = router['external_gateway_info']
                # Get all stateful (i.e. non-SLAAC/DHCPv6-stateless) fixed ips
                fips = [f for f in external_gateway_info['external_fixed_ips']
                        if not ipv6_utils.is_auto_address_subnet(
                            ext_subnets_dict[f['subnet_id']])]
                num_fips = len(fips)
                # Don't add the fixed IP to the port if it already
                # has a stateful fixed IP of the same IP version
                if num_fips > 1:
                    continue
                if num_fips == 1 and netaddr.IPAddress(
                        fips[0]['ip_address']).version == subnet['ip_version']:
                    continue
                external_gateway_info['external_fixed_ips'].append(
                                             {'subnet_id': subnet['id']})
                info = {'router': {'external_gateway_info':
                    external_gateway_info}}
                l3plugin.update_router(context, id, info)

    def _create_subnet(self, context, subnet, subnetpool_id):
        s = subnet['subnet']

        with context.session.begin(subtransactions=True):
            network = self._get_network(context, s["network_id"])
            subnet, ipam_subnet = self.ipam.allocate_subnet(context,
                                                            network,
                                                            s,
                                                            subnetpool_id)
        if hasattr(network, 'external') and network.external:
            self._update_router_gw_ports(context,
                                         network,
                                         subnet)
        # If this subnet supports auto-addressing, then update any
        # internal ports on the network with addresses for this subnet.
        if ipv6_utils.is_auto_address_subnet(subnet):
            updated_ports = self.ipam.add_auto_addrs_on_network_ports(context,
                                subnet, ipam_subnet)
            for port_id in updated_ports:
                port_info = {'port': {'id': port_id}}
                self.update_port(context, port_id, port_info)

        return self._make_subnet_dict(subnet, context=context)

    def _get_subnetpool_id(self, context, subnet):
        """Return the subnetpool id for this request

        :param subnet: The subnet dict from the request
        """
        use_default_subnetpool = subnet.get('use_default_subnetpool')
        if use_default_subnetpool == constants.ATTR_NOT_SPECIFIED:
            use_default_subnetpool = False
        subnetpool_id = subnet.get('subnetpool_id')
        if subnetpool_id == constants.ATTR_NOT_SPECIFIED:
            subnetpool_id = None

        if use_default_subnetpool and subnetpool_id:
            msg = _('subnetpool_id and use_default_subnetpool cannot both be '
                    'specified')
            raise exc.BadRequest(resource='subnets', msg=msg)

        if subnetpool_id:
            return subnetpool_id

        if not use_default_subnetpool:
            return

        cidr = subnet.get('cidr')
        if validators.is_attr_set(cidr):
            ip_version = netaddr.IPNetwork(cidr).version
        else:
            ip_version = subnet.get('ip_version')
            if not validators.is_attr_set(ip_version):
                msg = _('ip_version must be specified in the absence of '
                        'cidr and subnetpool_id')
                raise exc.BadRequest(resource='subnets', msg=msg)

        if ip_version == 6 and cfg.CONF.ipv6_pd_enabled:
            return constants.IPV6_PD_POOL_ID

        subnetpool = self.get_default_subnetpool(context, ip_version)
        if subnetpool:
            return subnetpool['id']

        msg = _('No default subnetpool found for IPv%s') % ip_version
        raise exc.BadRequest(resource='subnets', msg=msg)

    @db_api.retry_if_session_inactive()
    def create_subnet(self, context, subnet):

        s = subnet['subnet']
        cidr = s.get('cidr', constants.ATTR_NOT_SPECIFIED)
        prefixlen = s.get('prefixlen', constants.ATTR_NOT_SPECIFIED)
        has_cidr = validators.is_attr_set(cidr)
        has_prefixlen = validators.is_attr_set(prefixlen)

        if has_cidr and has_prefixlen:
            msg = _('cidr and prefixlen must not be supplied together')
            raise exc.BadRequest(resource='subnets', msg=msg)

        if has_cidr:
            # turn the CIDR into a proper subnet
            net = netaddr.IPNetwork(s['cidr'])
            subnet['subnet']['cidr'] = '%s/%s' % (net.network, net.prefixlen)

        subnetpool_id = self._get_subnetpool_id(context, s)
        if not subnetpool_id and not has_cidr:
            msg = _('a subnetpool must be specified in the absence of a cidr')
            raise exc.BadRequest(resource='subnets', msg=msg)

        if subnetpool_id:
            self.ipam.validate_pools_with_subnetpool(s)
            if subnetpool_id == constants.IPV6_PD_POOL_ID:
                if has_cidr:
                    # We do not currently support requesting a specific
                    # cidr with IPv6 prefix delegation. Set the subnetpool_id
                    # to None and allow the request to continue as normal.
                    subnetpool_id = None
                    self._validate_subnet(context, s)
                else:
                    prefix = n_const.PROVISIONAL_IPV6_PD_PREFIX
                    subnet['subnet']['cidr'] = prefix
                    self._validate_subnet_for_pd(s)
        else:
            if not has_cidr:
                msg = _('A cidr must be specified in the absence of a '
                        'subnet pool')
                raise exc.BadRequest(resource='subnets', msg=msg)
            self._validate_subnet(context, s)

        return self._create_subnet(context, subnet, subnetpool_id)

    def _update_allocation_pools(self, subnet):
        """Gets new allocation pools and formats them correctly"""
        allocation_pools = self.ipam.generate_pools(subnet['cidr'],
                                                    subnet['gateway_ip'])
        return [{'start': str(netaddr.IPAddress(p.first,
                                                subnet['ip_version'])),
                 'end': str(netaddr.IPAddress(p.last, subnet['ip_version']))}
                for p in allocation_pools]

    @db_api.retry_if_session_inactive()
    def update_subnet(self, context, id, subnet):
        """Update the subnet with new info.

        The change however will not be realized until the client renew the
        dns lease or we support gratuitous DHCP offers
        """
        orig = self.get_subnet(context, id)
        result = self._update_subnet_precommit(context, id, subnet)
        return self._update_subnet_postcommit(context, orig, result)

    def _update_subnet_precommit(self, context, id, subnet):
        """All subnet update operations safe to enclose in a transaction.

        :param context: neutron api request context
        :param id: subnet id
        :param subnet: API request dictionary
        """
        s = subnet['subnet']
        new_cidr = s.get('cidr')
        db_subnet = self._get_subnet(context, id)
        # Fill 'ip_version' and 'allocation_pools' fields with the current
        # value since _validate_subnet() expects subnet spec has 'ip_version'
        # and 'allocation_pools' fields.
        s['ip_version'] = db_subnet.ip_version
        s['cidr'] = db_subnet.cidr
        s['id'] = db_subnet.id
        s['tenant_id'] = db_subnet.tenant_id
        s['subnetpool_id'] = db_subnet.subnetpool_id
        self._validate_subnet(context, s, cur_subnet=db_subnet)
        db_pools = [netaddr.IPRange(p['first_ip'], p['last_ip'])
                    for p in db_subnet.allocation_pools]

        if new_cidr and ipv6_utils.is_ipv6_pd_enabled(s):
            # This is an ipv6 prefix delegation-enabled subnet being given an
            # updated cidr by the process_prefix_update RPC
            s['cidr'] = new_cidr
            net = netaddr.IPNetwork(s['cidr'], s['ip_version'])
            # Update gateway_ip and allocation pools based on new cidr
            s['gateway_ip'] = utils.get_first_host_ip(net, s['ip_version'])
            s['allocation_pools'] = self._update_allocation_pools(s)

        range_pools = None
        if s.get('allocation_pools') is not None:
            # Convert allocation pools to IPRange to simplify future checks
            range_pools = self.ipam.pools_to_ip_range(s['allocation_pools'])
            self.ipam.validate_allocation_pools(range_pools, s['cidr'])
            s['allocation_pools'] = range_pools

        # If either gateway_ip or allocation_pools were specified
        gateway_ip = s.get('gateway_ip', db_subnet.gateway_ip)
        gateway_ip_changed = gateway_ip != db_subnet.gateway_ip
        if gateway_ip_changed or s.get('allocation_pools') is not None:
            pools = range_pools if range_pools is not None else db_pools
            if gateway_ip:
                self.ipam.validate_gw_out_of_pools(gateway_ip, pools)

        if gateway_ip_changed:
            # Provide pre-update notification not to break plugins that don't
            # support gateway ip change
            kwargs = {'context': context, 'subnet_id': id,
                      'network_id': db_subnet.network_id}
            registry.notify(resources.SUBNET_GATEWAY, events.BEFORE_UPDATE,
                            self, **kwargs)

        with context.session.begin(subtransactions=True):
            subnet, changes = self.ipam.update_db_subnet(context, id, s,
                                                         db_pools)
        # we expire here since ipam may have made changes to relationships
        # that will be stale on any subsequent lookups while the subnet object
        # is in the session otherwise.
        context.session.expire(subnet)
        return self._make_subnet_dict(subnet, context=context)

    def _update_subnet_postcommit(self, context, orig, result):
        """Subnet update operations that happen after transaction completes.

        :param context: neutron api request context
        :param orig: subnet dictionary representing state before update
        :param result: subnet dictionary representing state after update
        """
        update_ports_needed = (result['cidr'] != orig['cidr'] and
                               ipv6_utils.is_ipv6_pd_enabled(result))
        if update_ports_needed:
            # Find ports that have not yet been updated
            # with an IP address by Prefix Delegation, and update them
            filters = {'fixed_ips': {'subnet_id': [result['id']]}}
            ports = self.get_ports(context, filters=filters)
            routers = []
            for port in ports:
                for ip in port['fixed_ips']:
                    if ip['subnet_id'] == result['id']:
                        if (port['device_owner'] in
                            constants.ROUTER_INTERFACE_OWNERS):
                            routers.append(port['device_id'])
                            ip['ip_address'] = result['gateway_ip']
                        else:
                            # We remove ip_address and pass only PD subnet_id
                            # in port's fixed_ip for port_update. Later, IPAM
                            # drivers will allocate eui64 address with new
                            # prefix when they find PD subnet_id in port's
                            # fixed_ip.
                            ip.pop('ip_address', None)
                self.update_port(context, port['id'], {'port': port})
            # Send router_update to l3_agent
            if routers:
                l3_rpc_notifier = l3_rpc_agent_api.L3AgentNotifyAPI()
                l3_rpc_notifier.routers_updated(context, routers)

        if orig['gateway_ip'] != result['gateway_ip']:
            kwargs = {'context': context, 'subnet_id': result['id'],
                      'network_id': result['network_id']}
            registry.notify(resources.SUBNET_GATEWAY, events.AFTER_UPDATE,
                            self, **kwargs)

        return result

    def _subnet_check_ip_allocations(self, context, subnet_id):
        return (context.session.query(models_v2.IPAllocation).
                filter_by(subnet_id=subnet_id).join(models_v2.Port).first())

    def _subnet_get_user_allocation(self, context, subnet_id):
        """Check if there are any user ports on subnet and return first."""
        # need to join with ports table as IPAllocation's port
        # is not joined eagerly and thus producing query which yields
        # incorrect results
        return (context.session.query(models_v2.IPAllocation).
                filter_by(subnet_id=subnet_id).join(models_v2.Port).
                filter(~models_v2.Port.device_owner.
                       in_(AUTO_DELETE_PORT_OWNERS)).first())

    def _subnet_check_ip_allocations_internal_router_ports(self, context,
                                                           subnet_id):
        # Do not delete the subnet if IP allocations for internal
        # router ports still exist
        allocs = context.session.query(models_v2.IPAllocation).filter_by(
                subnet_id=subnet_id).join(models_v2.Port).filter(
                        models_v2.Port.device_owner.in_(
                            constants.ROUTER_INTERFACE_OWNERS)
                ).first()
        if allocs:
            LOG.debug("Subnet %s still has internal router ports, "
                      "cannot delete", subnet_id)
            raise exc.SubnetInUse(subnet_id=subnet_id)

    @db_api.retry_if_session_inactive()
    def delete_subnet(self, context, id):
        with context.session.begin(subtransactions=True):
            subnet = self._get_subnet(context, id)

            # Make sure the subnet isn't used by other resources
            _check_subnet_not_used(context, id)

            # Delete all network owned ports
            qry_network_ports = (
                context.session.query(models_v2.IPAllocation).
                filter_by(subnet_id=subnet['id']).
                join(models_v2.Port))
            # Remove network owned ports, and delete IP allocations
            # for IPv6 addresses which were automatically generated
            # via SLAAC
            is_auto_addr_subnet = ipv6_utils.is_auto_address_subnet(subnet)
            if is_auto_addr_subnet:
                self._subnet_check_ip_allocations_internal_router_ports(
                        context, id)
            else:
                qry_network_ports = (
                    qry_network_ports.filter(models_v2.Port.device_owner.
                    in_(AUTO_DELETE_PORT_OWNERS)))
            network_ports = qry_network_ports.all()
            if network_ports:
                for port in network_ports:
                    context.session.delete(port)
            # Check if there are more IP allocations, unless
            # is_auto_address_subnet is True. In that case the check is
            # unnecessary. This additional check not only would be wasteful
            # for this class of subnet, but is also error-prone since when
            # the isolation level is set to READ COMMITTED allocations made
            # concurrently will be returned by this query
            if not is_auto_addr_subnet:
                alloc = self._subnet_check_ip_allocations(context, id)
                if alloc:
                    LOG.info(_LI("Found port (%(port_id)s, %(ip)s) having IP "
                                 "allocation on subnet "
                                 "%(subnet)s, cannot delete"),
                             {'ip': alloc.ip_address,
                              'port_id': alloc.port_id,
                              'subnet': id})
                    raise exc.SubnetInUse(subnet_id=id)

            context.session.delete(subnet)
            # Delete related ipam subnet manually,
            # since there is no FK relationship
            self.ipam.delete_subnet(context, id)

    @db_api.retry_if_session_inactive()
    def get_subnet(self, context, id, fields=None):
        subnet = self._get_subnet(context, id)
        return self._make_subnet_dict(subnet, fields, context=context)

    @db_api.retry_if_session_inactive()
    def get_subnets(self, context, filters=None, fields=None,
                    sorts=None, limit=None, marker=None,
                    page_reverse=False):
        return self._get_subnets(context, filters, fields, sorts, limit,
                                 marker, page_reverse)

    @db_api.retry_if_session_inactive()
    def get_subnets_count(self, context, filters=None):
        return self._get_collection_count(context, models_v2.Subnet,
                                          filters=filters)

    @db_api.retry_if_session_inactive()
    def get_subnets_by_network(self, context, network_id):
        return [self._make_subnet_dict(subnet_db) for subnet_db in
                self._get_subnets_by_network(context, network_id)]

    def _validate_address_scope_id(self, context, address_scope_id,
                                   subnetpool_id, sp_prefixes, ip_version):
        """Validate the address scope before associating.

        Subnetpool can associate with an address scope if
          - the tenant user is the owner of both the subnetpool and
            address scope
          - the admin is associating the subnetpool with the shared
            address scope
          - there is no prefix conflict with the existing subnetpools
            associated with the address scope.
          - the address family of the subnetpool and address scope
            are the same
        """
        if not validators.is_attr_set(address_scope_id):
            return

        if not self.is_address_scope_owned_by_tenant(context,
                                                     address_scope_id):
            raise n_exc.IllegalSubnetPoolAssociationToAddressScope(
                subnetpool_id=subnetpool_id, address_scope_id=address_scope_id)

        as_ip_version = self.get_ip_version_for_address_scope(context,
                                                              address_scope_id)

        if ip_version != as_ip_version:
            raise n_exc.IllegalSubnetPoolIpVersionAssociationToAddressScope(
                subnetpool_id=subnetpool_id, address_scope_id=address_scope_id,
                ip_version=as_ip_version)

        subnetpools = subnetpool_obj.SubnetPool.get_objects(
            context, address_scope_id=address_scope_id)

        new_set = netaddr.IPSet(sp_prefixes)
        for sp in subnetpools:
            if sp.id == subnetpool_id:
                continue
            sp_set = netaddr.IPSet(sp.prefixes)
            if sp_set.intersection(new_set):
                raise n_exc.AddressScopePrefixConflict()

    def _check_subnetpool_update_allowed(self, context, subnetpool_id,
                                         address_scope_id):
        """Check if the subnetpool can be updated or not.

        If the subnetpool is associated to a shared address scope not owned
        by the tenant, then the subnetpool cannot be updated.
        """

        if not self.is_address_scope_owned_by_tenant(context,
                                                     address_scope_id):
            msg = _("subnetpool %(subnetpool_id)s cannot be updated when"
                    " associated with shared address scope "
                    "%(address_scope_id)s") % {
                        'subnetpool_id': subnetpool_id,
                        'address_scope_id': address_scope_id}
            raise n_exc.IllegalSubnetPoolUpdate(reason=msg)

    def _check_default_subnetpool_exists(self, context, ip_version):
        """Check if a default already exists for the given IP version.

        There can only be one default subnetpool for each IP family. Raise an
        InvalidInput error if a default has already been set.
        """
        if self.get_default_subnetpool(context, ip_version):
            msg = _("A default subnetpool for this IP family has already "
                    "been set. Only one default may exist per IP family")
            raise exc.InvalidInput(error_message=msg)

    @db_api.retry_if_session_inactive()
    def create_subnetpool(self, context, subnetpool):
        sp = subnetpool['subnetpool']
        sp_reader = subnet_alloc.SubnetPoolReader(sp)
        if sp_reader.address_scope_id is constants.ATTR_NOT_SPECIFIED:
            sp_reader.address_scope_id = None
        if sp_reader.is_default:
            self._check_default_subnetpool_exists(context,
                                                  sp_reader.ip_version)
        self._validate_address_scope_id(context, sp_reader.address_scope_id,
                                        id, sp_reader.prefixes,
                                        sp_reader.ip_version)
        pool_args = {'tenant_id': sp['tenant_id'],
                     'id': sp_reader.id,
                     'name': sp_reader.name,
                     'ip_version': sp_reader.ip_version,
                     'default_prefixlen':
                     sp_reader.default_prefixlen,
                     'min_prefixlen': sp_reader.min_prefixlen,
                     'max_prefixlen': sp_reader.max_prefixlen,
                     'is_default': sp_reader.is_default,
                     'shared': sp_reader.shared,
                     'default_quota': sp_reader.default_quota,
                     'address_scope_id': sp_reader.address_scope_id,
                     'description': sp_reader.description,
                     'prefixes': sp_reader.prefixes}
        subnetpool = subnetpool_obj.SubnetPool(context, **pool_args)
        subnetpool.create()

        return self._make_subnetpool_dict(subnetpool.db_obj)

    @db_api.retry_if_session_inactive()
    def update_subnetpool(self, context, id, subnetpool):
        new_sp = subnetpool['subnetpool']

        with context.session.begin(subtransactions=True):
            orig_sp = self._get_subnetpool(context, id=id)
            updated = _update_subnetpool_dict(orig_sp, new_sp)
            reader = subnet_alloc.SubnetPoolReader(updated)
            if reader.is_default and not orig_sp.is_default:
                self._check_default_subnetpool_exists(context,
                                                      reader.ip_version)
            if orig_sp.address_scope_id:
                self._check_subnetpool_update_allowed(context, id,
                                                      orig_sp.address_scope_id)

            self._validate_address_scope_id(context, reader.address_scope_id,
                                            id, reader.prefixes,
                                            reader.ip_version)
            address_scope_changed = (
                orig_sp.address_scope_id != reader.address_scope_id)

            orig_sp.update_fields(reader.subnetpool)
            orig_sp.update()

        if address_scope_changed:
            # Notify about the update of subnetpool's address scope
            kwargs = {'context': context, 'subnetpool_id': id}
            registry.notify(resources.SUBNETPOOL_ADDRESS_SCOPE,
                            events.AFTER_UPDATE,
                            self.update_subnetpool,
                            **kwargs)

        for key in ['min_prefixlen', 'max_prefixlen', 'default_prefixlen']:
            updated['key'] = str(updated[key])
        self._apply_dict_extend_functions(attributes.SUBNETPOOLS,
                                          updated, orig_sp.db_obj)
        return updated

    @db_api.retry_if_session_inactive()
    def get_subnetpool(self, context, id, fields=None):
        subnetpool = self._get_subnetpool(context, id)
        return self._make_subnetpool_dict(subnetpool.db_obj, fields)

    @db_api.retry_if_session_inactive()
    def get_subnetpools(self, context, filters=None, fields=None,
                        sorts=None, limit=None, marker=None,
                        page_reverse=False):
        pager = base_obj.Pager(sorts, limit, page_reverse, marker)
        subnetpools = subnetpool_obj.SubnetPool.get_objects(
            context, _pager=pager, validate_filters=False, **filters)
        return [
            self._make_subnetpool_dict(pool.db_obj, fields)
            for pool in subnetpools
        ]

    @db_api.retry_if_session_inactive()
    def get_default_subnetpool(self, context, ip_version):
        """Retrieve the default subnetpool for the given IP version."""
        filters = {'is_default': True,
                   'ip_version': ip_version}
        subnetpool = self.get_subnetpools(context, filters=filters)
        if subnetpool:
            return subnetpool[0]

    @db_api.retry_if_session_inactive()
    def delete_subnetpool(self, context, id):
        with context.session.begin(subtransactions=True):
            subnetpool = self._get_subnetpool(context, id=id)
            subnets = self._get_subnets_by_subnetpool(context, id)
            if subnets:
                reason = _("Subnet pool has existing allocations")
                raise n_exc.SubnetPoolDeleteError(reason=reason)
            subnetpool.delete()

    def _check_mac_addr_update(self, context, port, new_mac, device_owner):
        if (device_owner and
            device_owner.startswith(constants.DEVICE_OWNER_NETWORK_PREFIX)):
            raise n_exc.UnsupportedPortDeviceOwner(
                op=_("mac address update"), port_id=id,
                device_owner=device_owner)

    @db_api.retry_if_session_inactive()
    def create_port_bulk(self, context, ports):
        return self._create_bulk('port', context, ports)

    def _create_db_port_obj(self, context, port_data):
        mac_address = port_data.pop('mac_address', None)
        if mac_address:
            if self._is_mac_in_use(context, port_data['network_id'],
                                   mac_address):
                raise exc.MacAddressInUse(net_id=port_data['network_id'],
                                          mac=mac_address)
        else:
            mac_address = self._generate_mac()
        db_port = models_v2.Port(mac_address=mac_address, **port_data)
        context.session.add(db_port)
        return db_port

    @db_api.retry_if_session_inactive()
    def create_port(self, context, port):
        db_port = self.create_port_db(context, port)
        return self._make_port_dict(db_port, process_extensions=False)

    def create_port_db(self, context, port):
        p = port['port']
        port_id = p.get('id') or uuidutils.generate_uuid()
        network_id = p['network_id']
        # NOTE(jkoelker) Get the tenant_id outside of the session to avoid
        #                unneeded db action if the operation raises
        tenant_id = p['tenant_id']
        if p.get('device_owner'):
            self._enforce_device_owner_not_router_intf_or_device_id(
                context, p.get('device_owner'), p.get('device_id'), tenant_id)

        port_data = dict(tenant_id=tenant_id,
                         name=p['name'],
                         id=port_id,
                         network_id=network_id,
                         admin_state_up=p['admin_state_up'],
                         status=p.get('status', constants.PORT_STATUS_ACTIVE),
                         device_id=p['device_id'],
                         device_owner=p['device_owner'],
                         description=p.get('description'))
        if p.get('mac_address') is not constants.ATTR_NOT_SPECIFIED:
            port_data['mac_address'] = p.get('mac_address')
        with context.session.begin(subtransactions=True):
            # Ensure that the network exists.
            self._get_network(context, network_id)

            # Create the port
            db_port = self._create_db_port_obj(context, port_data)
            p['mac_address'] = db_port['mac_address']

            try:
                self.ipam.allocate_ips_for_port_and_store(
                    context, port, port_id)
                db_port['ip_allocation'] = ipa.IP_ALLOCATION_IMMEDIATE
            except ipam_exc.DeferIpam:
                db_port['ip_allocation'] = ipa.IP_ALLOCATION_DEFERRED
            fixed_ips = p['fixed_ips']
            if validators.is_attr_set(fixed_ips) and not fixed_ips:
                # [] was passed explicitly as fixed_ips. An unaddressed port.
                db_port['ip_allocation'] = ipa.IP_ALLOCATION_NONE

        return db_port

    def _validate_port_for_update(self, context, db_port, new_port, new_mac):
        changed_owner = 'device_owner' in new_port
        current_owner = (new_port.get('device_owner') or
                         db_port['device_owner'])
        changed_device_id = new_port.get('device_id') != db_port['device_id']
        current_device_id = new_port.get('device_id') or db_port['device_id']

        if current_owner and changed_device_id or changed_owner:
            self._enforce_device_owner_not_router_intf_or_device_id(
                context, current_owner, current_device_id,
                db_port['tenant_id'])

        if new_mac and new_mac != db_port['mac_address']:
            self._check_mac_addr_update(context, db_port,
                                        new_mac, current_owner)

    @db_api.retry_if_session_inactive()
    def update_port(self, context, id, port):
        new_port = port['port']

        with context.session.begin(subtransactions=True):
            db_port = self._get_port(context, id)
            new_mac = new_port.get('mac_address')
            self._validate_port_for_update(context, db_port, new_port, new_mac)
            # Note: _make_port_dict is called here to load extension data
            # (specifically host binding).  The IPAM plugin is separate from
            # the core plugin, so extensions are not loaded.
            #
            # The IPAM code could cheat and get it directly from db_port but it
            # would have to know about the implementation (remember ml2 has its
            # own port binding schema that differs from the generic one)
            #
            # This code could extract just the port binding host here and pass
            # that in.  The problem is that db_base_plugin_common shouldn't
            # know anything about port binding.  This compromise sends IPAM a
            # port_dict with all of the extension data loaded.
            try:
                self.ipam.update_port(
                    context,
                    old_port_db=db_port,
                    old_port=self._make_port_dict(db_port),
                    new_port=new_port)
            except ipam_exc.IpAddressAllocationNotFound as e:
                # If a port update and a subnet delete interleave, there is a
                # chance that the IPAM update operation raises this exception.
                # Rather than throwing that up to the user under some sort of
                # conflict, bubble up a retry instead that should bring things
                # back to sanity.
                raise os_db_exc.RetryRequest(e)
        result = self._make_port_dict(db_port)
        return result

    @db_api.retry_if_session_inactive()
    def delete_port(self, context, id):
        with context.session.begin(subtransactions=True):
            self.ipam.delete_port(context, id)

    def delete_ports_by_device_id(self, context, device_id, network_id=None):
        query = (context.session.query(models_v2.Port.id)
                 .enable_eagerloads(False)
                 .filter(models_v2.Port.device_id == device_id))
        if network_id:
            query = query.filter(models_v2.Port.network_id == network_id)
        port_ids = [p[0] for p in query]
        for port_id in port_ids:
            try:
                self.delete_port(context, port_id)
            except exc.PortNotFound:
                # Don't raise if something else concurrently deleted the port
                LOG.debug("Ignoring PortNotFound when deleting port '%s'. "
                          "The port has already been deleted.",
                          port_id)

    @db_api.retry_if_session_inactive()
    def get_port(self, context, id, fields=None):
        port = self._get_port(context, id)
        return self._make_port_dict(port, fields)

    def _get_ports_query(self, context, filters=None, sorts=None, limit=None,
                         marker_obj=None, page_reverse=False):
        Port = models_v2.Port
        IPAllocation = models_v2.IPAllocation

        if not filters:
            filters = {}

        query = self._model_query(context, Port)

        fixed_ips = filters.pop('fixed_ips', {})
        ip_addresses = fixed_ips.get('ip_address')
        subnet_ids = fixed_ips.get('subnet_id')
        if ip_addresses or subnet_ids:
            query = query.join(Port.fixed_ips)
            if ip_addresses:
                query = query.filter(IPAllocation.ip_address.in_(ip_addresses))
            if subnet_ids:
                query = query.filter(IPAllocation.subnet_id.in_(subnet_ids))

        query = self._apply_filters_to_query(query, Port, filters, context)
        if sorts:
            sort_keys = db_utils.get_and_validate_sort_keys(sorts, Port)
            sort_dirs = db_utils.get_sort_dirs(sorts, page_reverse)
            query = sa_utils.paginate_query(query, Port, limit,
                                            marker=marker_obj,
                                            sort_keys=sort_keys,
                                            sort_dirs=sort_dirs)
        return query

    @db_api.retry_if_session_inactive()
    def get_ports(self, context, filters=None, fields=None,
                  sorts=None, limit=None, marker=None,
                  page_reverse=False):
        marker_obj = self._get_marker_obj(context, 'port', limit, marker)
        query = self._get_ports_query(context, filters=filters,
                                      sorts=sorts, limit=limit,
                                      marker_obj=marker_obj,
                                      page_reverse=page_reverse)
        items = [self._make_port_dict(c, fields) for c in query]
        if limit and page_reverse:
            items.reverse()
        return items

    @db_api.retry_if_session_inactive()
    def get_ports_count(self, context, filters=None):
        return self._get_ports_query(context, filters).count()

    def _enforce_device_owner_not_router_intf_or_device_id(self, context,
                                                           device_owner,
                                                           device_id,
                                                           tenant_id):
        """Prevent tenants from replacing the device id of router ports with
        a router uuid belonging to another tenant.
        """
        if device_owner not in constants.ROUTER_INTERFACE_OWNERS:
            return
        if not context.is_admin:
            # check to make sure device_id does not match another tenants
            # router.
            if device_id:
                if hasattr(self, 'get_router'):
                    try:
                        ctx_admin = context.elevated()
                        router = self.get_router(ctx_admin, device_id)
                    except l3.RouterNotFound:
                        return
                else:
                    l3plugin = (
                        manager.NeutronManager.get_service_plugins().get(
                            service_constants.L3_ROUTER_NAT))
                    if l3plugin:
                        try:
                            ctx_admin = context.elevated()
                            router = l3plugin.get_router(ctx_admin,
                                                         device_id)
                        except l3.RouterNotFound:
                            return
                    else:
                        # raise as extension doesn't support L3 anyways.
                        raise n_exc.DeviceIDNotOwnedByTenant(
                            device_id=device_id)
                if tenant_id != router['tenant_id']:
                    raise n_exc.DeviceIDNotOwnedByTenant(device_id=device_id)

    db_base_plugin_common.DbBasePluginCommon.register_model_query_hook(
        models_v2.Port,
        "port",
        '_port_query_hook',
        '_port_filter_hook',
        None)
