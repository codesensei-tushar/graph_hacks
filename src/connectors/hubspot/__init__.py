"""HubSpot: customer accounts, and who owns and works them. See docs/HUBSPOT.md."""

from src.connectors import registry
from src.connectors.hubspot.connector import HubSpotConnector

registry.register(HubSpotConnector())
