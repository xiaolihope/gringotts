#!/usr/bin/python
"""Plugins for executing specific actions acrronding to notification events.
"""
from oslo_config import cfg
from stevedore import extension

from gringotts import constants as const
from gringotts import utils as gringutils
from gringotts import exception
from gringotts import plugin
from gringotts.waiter import plugin as waiter_plugin
from gringotts.waiter.plugin import Collection
from gringotts.waiter.plugin import Order

from gringotts import services
from gringotts.services import keystone as ks_client

from gringotts.openstack.common import log
from gringotts.openstack.common import uuidutils


LOG = log.getLogger(__name__)


OPTS = [
    cfg.StrOpt('manila_control_exchange',
               default='manila',
               help="Exchange name for Manila notifications"),
]


cfg.CONF.register_opts(OPTS)


class SizeItem(waiter_plugin.ProductItem):

    def get_collection(self, message):
        """Get collection from message
        """
        product_name = const.PRODUCT_SHARE
        service = const.SERVICE_SHARE
        region_id = cfg.CONF.region_name
        resource_id = message['payload']['share_id']
        resource_name = message['payload']['display_name']
        resource_type = const.RESOURCE_SHARE
        resource_volume = message['payload']['size']
        user_id = message['payload']['user_id']
        project_id = message['payload']['tenant_id']

        return Collection(product_name=product_name,
                          service=service,
                          region_id=region_id,
                          resource_id=resource_id,
                          resource_name=resource_name,
                          resource_type=resource_type,
                          resource_volume=resource_volume,
                          user_id=user_id,
                          project_id=project_id)


class ShareNotificationBase(waiter_plugin.NotificationBase):

    def __init__(self):
        super(ShareNotificationBase, self).__init__()
        self.product_items = extension.ExtensionManager(
            namespace='gringotts.share.product_item',
            invoke_on_load=True,
        )

    @staticmethod
    def get_exchange_topics(conf):
        """Return a sequence of ExchangeTopics defining the exchange and
        topics to be connected for this plugin.
        """
        return [
            plugin.ExchangeTopics(
                exchange=conf.manila_control_exchange,
                topics=set(topic + ".info"
                           for topic in conf.notification_topics)),
        ]

    def make_order(self, message, state=None):
        """Make an order model for one share
        """
        resource_id = message['payload']['share_id']
        resource_name = message['payload']['display_name']
        user_id = message['payload']['user_id']
        project_id = message['payload']['tenant_id']

        order = Order(resource_id=resource_id,
                      resource_name=resource_name,
                      type=const.RESOURCE_SHARE,
                      status=state if state else const.STATE_RUNNING,
                      user_id=user_id,
                      project_id=project_id)
        return order


class ShareCreateEnd(ShareNotificationBase):
    """Handle the event that share be created
    """
    event_types = ['share.create.end']

    def process_notification(self, message, state=None):
        LOG.warn('Do action for event: %s, resource_id: %s',
                 message['event_type'],
                 message['payload']['share_id'])

        # Generate uuid of an order
        order_id = uuidutils.generate_uuid()

        unit_price = 0
        unit = 'hour'

        # Create subscriptions for this order
        for ext in self.product_items.extensions:
            if ext.name.startswith('suspend'):
                sub = ext.obj.create_subscription(message, order_id,
                                                  type=const.STATE_SUSPEND)
                if sub and state==const.STATE_SUSPEND:
                    p = gringutils._quantize_decimal(sub['unit_price'])
                    unit_price += p * sub['quantity']
            elif ext.name.startswith('running'):
                sub = ext.obj.create_subscription(message, order_id,
                                                  type=const.STATE_RUNNING)
                if sub and (not state or state==const.STATE_RUNNING):
                    p = gringutils._quantize_decimal(sub['unit_price'])
                    unit_price += p * sub['quantity']

        # Create an order for this volume
        self.create_order(order_id, unit_price, unit, message, state=state)

        # Notify master
        remarks = 'Share Has Been Created.'
        action_time = message['timestamp']
        if state:
            self.resource_created_again(order_id, action_time, remarks)
        else:
            self.resource_created(order_id, action_time, remarks)

    def get_unit_price(self, order_id, message, status, cron_time=None):
        unit_price = 0

        # Create subscriptions for this order
        for ext in self.product_items.extensions:
            if ext.name.startswith(status):
                unit_price += ext.obj.get_unit_price(order_id, message)

        return unit_price

    def change_unit_price(self, message, status, order_id):
        quantity = message['payload']['size']
        self.change_order_unit_price(order_id, quantity, status)


services.register_class(ks_client,
                        'share',
                        const.RESOURCE_SHARE,
                        ShareCreateEnd)


class ShareDeleteEnd(ShareNotificationBase):
    """Handle the event that volume be deleted
    """
    event_types = ['share.delete.end']

    def process_notification(self, message):
        LOG.warn('Do action for event: %s, resource_id: %s',
                 message['event_type'],
                 message['payload']['share_id'])

        # Get the order of this resource
        resource_id = message['payload']['share_id']
        order = self.get_order_by_resource_id(resource_id)

        if order.get('unit') in ['month', 'year']:
            return

        # Notify master
        action_time = message['timestamp']
        remarks = 'Share Has Been Deleted'
        self.resource_deleted(order['order_id'], action_time, remarks)
