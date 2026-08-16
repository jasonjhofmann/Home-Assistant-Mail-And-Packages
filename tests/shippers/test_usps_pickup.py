"""Tests for USPS pickup sensor processing via generic shipper."""

from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.mail_and_packages.const import INBOUND_PICKUP_SENSORS
from custom_components.mail_and_packages.shippers.generic import GenericShipper

USPS_PICKUP_SUBJECT = "USPS - Your Package Pickup Request"

# One label that appears both on the pickup request and on an inbound delivery
# notice. USPS numbers all come from one namespace, so this collision is
# ordinary — the two sensors simply describe opposite directions of travel.
SHARED_TRACKING = "9400100000000000000001"


def _usps_notice(subject: str, body_html: str) -> bytes:
    r"""Build a minimal USPS notice.

    Single-part on purpose: the USPS tracking pattern ("9[2345]\d{15,26}") is
    matched against the RAW message, and the random digit run in a multipart
    boundary can satisfy it — which would silently make the extracted number
    something other than the one this notice is supposed to carry.
    """
    msg = MIMEText(f"<html><body>{body_html}</body></html>", "html")
    msg["From"] = "USPS <auto-reply@usps.com>"
    msg["Subject"] = subject
    msg["Date"] = "Wed, 12 Aug 2026 09:00:00 -0400"
    return msg.as_bytes()


@pytest.mark.asyncio
async def test_usps_pickup_email_generic_shipper(hass):
    """Test parsing of USPS Scheduled Pickup email via GenericShipper."""
    shipper = GenericShipper(hass, {})

    msg = MIMEMultipart("alternative")
    msg["From"] = "auto-reply@usps.com"
    msg["Subject"] = "USPS - Your Package Pickup Request"
    msg["Date"] = "Wed, 12 Aug 2026 12:46:26 -0400"
    html_body = """
    <html>
      <body>
        <p>Thank you for using USPS.com. We have successfully completed your Package Pickup.</p>
        <p>Confirmation #: WEC000000000</p>
        <p>Total Packages: 50</p>
        <p>Scheduled Pickup Date: 08/12/2026</p>
      </body>
    </html>
    """
    msg.attach(MIMEText(html_body, "html"))

    mock_account = AsyncMock()

    with (
        patch(
            "custom_components.mail_and_packages.shippers.generic.email_search",
            return_value=("OK", [b"1"]),
        ),
        patch(
            "custom_components.mail_and_packages.shippers.generic.email_fetch",
            return_value=("OK", [msg.as_bytes()]),
        ),
        patch(
            "custom_components.mail_and_packages.utils.email.email_fetch",
            return_value=("OK", [msg.as_bytes()]),
        ),
        patch(
            "custom_components.mail_and_packages.shippers.generic.email_fetch_headers",
            return_value=("OK", [b"Subject: USPS - Your Package Pickup Request\r\n"]),
        ),
    ):
        result = await shipper.process(
            account=mock_account,
            date="12-Aug-2026",
            sensor_type="usps_pickup",
        )

    assert result["count"] == 50


@pytest.mark.asyncio
async def test_usps_pickup_publishes_own_tracking_in_batch(hass):
    """usps_pickup must publish its own tracking key through the batch pipeline.

    The pickup confirmation lists the labels USPS is coming to collect. Those
    numbers belong to this sensor alone: the shipper-wide "usps_tracking" list
    holds parcels in transit TO the user, so an entity falling back to it would
    show entirely the wrong parcels. The batch also proves the pickup email
    cannot bleed into a sibling USPS sensor.
    """
    shipper = GenericShipper(hass, {})

    msg = MIMEMultipart("alternative")
    msg["From"] = "auto-reply@usps.com"
    msg["Subject"] = USPS_PICKUP_SUBJECT
    msg["Date"] = "Wed, 12 Aug 2026 12:46:26 -0400"
    html_body = """
    <html>
      <body>
        <p>Thank you for using USPS.com. We have successfully completed your Package Pickup.</p>
        <p>Confirmation #: WEC000000000</p>
        <p>Total Packages: 1</p>
        <p>Label: 9400100000000000000001</p>
        <p>Scheduled Pickup Date: 08/12/2026</p>
      </body>
    </html>
    """
    msg.attach(MIMEText(html_body, "html"))

    mock_account = AsyncMock()

    with (
        patch(
            "custom_components.mail_and_packages.shippers.generic.email_search",
            return_value=("OK", [b"1"]),
        ),
        patch(
            "custom_components.mail_and_packages.shippers.generic.email_fetch",
            return_value=("OK", [msg.as_bytes()]),
        ),
        patch(
            "custom_components.mail_and_packages.utils.email.email_fetch",
            return_value=("OK", [msg.as_bytes()]),
        ),
        patch(
            "custom_components.mail_and_packages.utils.shipper.email_fetch",
            return_value=("OK", [msg.as_bytes()]),
        ),
        patch(
            "custom_components.mail_and_packages.shippers.generic.email_fetch_headers",
            return_value=("OK", [f"Subject: {USPS_PICKUP_SUBJECT}\r\n".encode()]),
        ),
    ):
        result = await shipper.process_batch(
            mock_account,
            "12-Aug-2026",
            ["usps_pickup", "usps_delivered"],
            None,
            since_date="05-Aug-2026",
        )

    assert result["usps_pickup"] == 1
    assert result["usps_pickup_tracking"] == [SHARED_TRACKING]
    # The pickup email matches no other USPS sensor, and pickup stays out of the
    # coordinator's in-transit state machine.
    assert result["usps_delivered"] == 0
    assert "usps_pickup" not in result.get("_tracking_details", {})


@pytest.mark.asyncio
async def test_usps_pickup_search_stays_on_today(hass):
    """usps_pickup keeps its today-only window even when a since_date is given.

    It confirms an OUTBOUND collection the user scheduled: one confirmation
    means "USPS is coming to my address today", not "n parcels are still
    waiting somewhere". It therefore has to reset at midnight like _delivered.
    Widening it to the extended window — as the INBOUND_PICKUP_SENSORS need —
    would turn an already-shipped daily sensor into a rolling multi-day count,
    so the widening is keyed on membership rather than on the "_pickup" suffix.
    """
    shipper = GenericShipper(hass, {})
    mock_account = AsyncMock()

    with patch(
        "custom_components.mail_and_packages.shippers.generic.email_search",
        return_value=("OK", [None]),
    ) as mock_search:
        await shipper.process(
            mock_account, "22-Apr-2026", "usps_pickup", since_date="19-Apr-2026"
        )

    mock_search.assert_called_once()
    assert mock_search.call_args.kwargs["date"] == "22-Apr-2026"
    assert "usps_pickup" not in INBOUND_PICKUP_SENSORS


@pytest.mark.asyncio
async def test_usps_pickup_is_not_deduplicated_against_delivered(
    hass, mock_imap_mailbox
):
    """An inbound delivery must not cancel the user's outbound pickup request.

    The delivered-dedup pass exists to drop parcels the user has already
    received. usps_pickup counts the opposite direction — parcels USPS collects
    FROM the user — so a delivered notice says nothing about it. Both sensors
    read the same USPS tracking-number namespace, so wiring usps_pickup into
    that pass would let an unrelated inbound delivery of a colliding number
    zero a pickup that is genuinely scheduled.
    """
    account = mock_imap_mailbox(
        {
            "1": _usps_notice(
                USPS_PICKUP_SUBJECT,
                f"<p>Total Packages: 1</p><p>Label: {SHARED_TRACKING}</p>",
            ),
            "2": _usps_notice(
                "Item Delivered", f"<p>Your item {SHARED_TRACKING} was delivered.</p>"
            ),
        },
    )
    shipper = GenericShipper(hass, {})

    result = await shipper.process_batch(
        account,
        "12-Aug-2026",
        ["usps_pickup", "usps_delivered"],
        None,
        since_date="05-Aug-2026",
    )

    # Both emails were understood, so the dedup pass really did have this
    # tracking number in its "delivered" set
    assert result["usps_delivered"] == 1
    assert result["_tracking_details"]["usps_delivered"] == [SHARED_TRACKING]
    # ...and the pickup request survived it intact
    assert result["usps_pickup"] == 1
    assert result["usps_pickup_tracking"] == [SHARED_TRACKING]
