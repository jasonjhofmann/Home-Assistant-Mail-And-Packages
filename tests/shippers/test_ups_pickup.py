"""Tests for the UPS ready-for-pickup sensor via the generic shipper."""

import re
from email.mime.text import MIMEText
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.mail_and_packages.const import (
    AMAZON_HUB_SUBJECT,
    ATTR_COUNT,
    ATTR_SUBJECT,
    ATTR_TRACKING,
    INBOUND_PICKUP_SENSORS,
    SENSOR_DATA,
)
from custom_components.mail_and_packages.shippers.generic import GenericShipper

# Every UPS sensor that discriminates on subject. A UPS subject has to land in
# exactly one of them, so they are checked as a group.
UPS_SUBJECT_SENSORS = (
    "ups_delivered",
    "ups_delivering",
    "ups_exception",
    "ups_packages",
    "ups_pickup",
)

# Tracking numbers used by the multi-sensor batch fixtures below. PICKUP_TRACKING
# is the number carried by tests/test_emails/ups_ready_for_pickup.eml.
PICKUP_TRACKING = "1Z9999W99999999999"
DELIVERING_TRACKING = "1Z7777V77777777777"
OTHER_TRACKING = "1Z6666U66666666666"


def _ups_notice(subject: str, tracking: str) -> bytes:
    """Build a minimal UPS notice carrying a single tracking number.

    Single-part on purpose: the UPS tracking pattern ("1Z?[0-9A-Z]{16}") is
    matched against the RAW message, and the random digit run in a multipart
    boundary can satisfy it — which would silently make the extracted number
    something other than the one this notice is supposed to carry.
    """
    msg = MIMEText(
        f"<html><body><p>Tracking Number: {tracking}</p></body></html>", "html"
    )
    msg["From"] = "UPS <mcinfo@ups.com>"
    msg["Subject"] = subject
    msg["Date"] = "Wed, 12 Aug 2026 09:00:00 -0400"
    return msg.as_bytes()


def _recorded_pickup_notice() -> bytes:
    """Return the recorded "UPS - Package Ready for Pickup" message."""
    return Path("tests/test_emails/ups_ready_for_pickup.eml").read_bytes()


@pytest.mark.asyncio
async def test_ups_ready_for_pickup(hass, mock_imap_ups_ready_for_pickup):
    """Test the pkginfo@ups.com "UPS - Package Ready for Pickup" variant."""
    shipper = GenericShipper(hass, {"image_path": "test/path/"})

    result = await shipper.process(
        mock_imap_ups_ready_for_pickup,
        "today",
        "ups_pickup",
    )
    assert result[ATTR_COUNT] == 1
    assert result[ATTR_TRACKING] == [PICKUP_TRACKING]


@pytest.mark.asyncio
async def test_ups_my_choice_ready_for_pickup(
    hass, mock_imap_ups_my_choice_ready_for_pickup
):
    """Test the mcinfo@ups.com "UPS My Choice - Package Ready for Pickup" variant."""
    shipper = GenericShipper(hass, {"image_path": "test/path/"})

    result = await shipper.process(
        mock_imap_ups_my_choice_ready_for_pickup,
        "today",
        "ups_pickup",
    )
    assert result[ATTR_COUNT] == 1
    assert result[ATTR_TRACKING] == ["1Z8888X88888888888"]


@pytest.mark.asyncio
async def test_ups_pickup_email_not_counted_as_delivering(
    hass, mock_imap_ups_ready_for_pickup
):
    """A ready-for-pickup email must not inflate the out-for-delivery sensor."""
    shipper = GenericShipper(hass, {"image_path": "test/path/"})

    result = await shipper.process(
        mock_imap_ups_ready_for_pickup,
        "today",
        "ups_delivering",
    )
    assert result[ATTR_COUNT] == 0
    assert result[ATTR_TRACKING] == []


@pytest.mark.asyncio
async def test_ups_delivered_email_not_counted_as_pickup(hass, mock_imap_ups_delivered):
    """An ordinary UPS delivery email must not match the pickup sensor."""
    shipper = GenericShipper(hass, {"image_path": "test/path/"})

    result = await shipper.process(
        mock_imap_ups_delivered,
        "today",
        "ups_pickup",
    )
    assert result[ATTR_COUNT] == 0
    assert result[ATTR_TRACKING] == []


@pytest.mark.asyncio
async def test_ups_pickup_search_uses_since_date(hass):
    """The pickup search must use the extended window, not today only.

    UPS sends exactly ONE "ready for pickup" notice and then holds the parcel
    at the Access Point for about five business days. A today-only search would
    report the parcel on the day the notice arrived and 0 for the rest of the
    hold — the opposite of what the sensor promises.
    """
    shipper = GenericShipper(hass, {})
    mock_account = AsyncMock()

    with patch(
        "custom_components.mail_and_packages.shippers.generic.email_search",
        return_value=("OK", [None]),
    ) as mock_search:
        await shipper.process(
            mock_account, "22-Apr-2026", "ups_pickup", since_date="19-Apr-2026"
        )

    mock_search.assert_called_once()
    assert mock_search.call_args.kwargs["date"] == "19-Apr-2026"
    # The widening is keyed on membership, not on the "_pickup" suffix, so that
    # the outbound usps_pickup sensor keeps its today-only window.
    assert "ups_pickup" in INBOUND_PICKUP_SENSORS


@pytest.mark.asyncio
async def test_ups_pickup_search_is_scoped_to_ups_senders(
    hass, mock_imap_ups_ready_for_pickup
):
    """The emitted IMAP criteria must scope the search to the two UPS senders.

    "Package Ready for Pickup" is a phrase plenty of non-carriers use, so the
    FROM clause carries real weight. The mocked account matches every SEARCH
    regardless of criteria, so the only way to pin the sender list down is to
    inspect the query that was actually sent.
    """
    shipper = GenericShipper(hass, {"image_path": "test/path/"})

    await shipper.process(mock_imap_ups_ready_for_pickup, "today", "ups_pickup")

    query = mock_imap_ups_ready_for_pickup.search.call_args.args[0]
    assert set(re.findall(r'FROM "([^"]+)"', query)) == {
        "mcinfo@ups.com",
        "pkginfo@ups.com",
    }
    assert 'SUBJECT "Package Ready for Pickup"' in query


@pytest.mark.asyncio
async def test_ups_pickup_publishes_own_tracking_list(
    hass, mock_imap_ups_ready_for_pickup
):
    """Pickup results carry a per-sensor tracking list and stay out of the state machine.

    "ups_pickup_tracking" is what the entity reads for its tracking_# attribute;
    "_tracking_details" is what the coordinator feeds into the in-transit state
    machine, and pickup must never appear there (a parcel waiting at an Access
    Point is neither in transit nor delivered). Publishing the shipper-wide
    "ups_tracking" key instead would hand the entity the in-transit list.
    """
    shipper = GenericShipper(hass, {"image_path": "test/path/"})

    result = await shipper.process_batch(
        mock_imap_ups_ready_for_pickup,
        "today",
        ["ups_pickup"],
        None,
    )
    assert result["ups_pickup"] == 1
    assert result["ups_pickup_tracking"] == [PICKUP_TRACKING]
    assert "_tracking_details" not in result
    assert [key for key in result if key.endswith("_tracking")] == [
        "ups_pickup_tracking"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("delivered_tracking", "expected_count", "expected_tracking"),
    [
        (PICKUP_TRACKING, 0, []),
        (OTHER_TRACKING, 1, [PICKUP_TRACKING]),
    ],
    ids=["collected", "still-waiting"],
)
async def test_pickup_deduplicates_against_delivered_in_batch(
    hass,
    mock_imap_mailbox,
    delivered_tracking,
    expected_count,
    expected_tracking,
):
    """Collecting the parcel must clear the pickup sensor, and only then.

    UPS sends a delivered notice once the parcel is handed over at the Access
    Point, but the ready-for-pickup email stays inside the extended search
    window for days afterwards. Without deduplication the sensor would keep
    counting a parcel the user is already holding. A delivered notice for some
    other parcel must leave it alone.
    """
    account = mock_imap_mailbox(
        {
            "1": _recorded_pickup_notice(),
            "2": _ups_notice(
                "UPS Update: Package Scheduled for Delivery Today", DELIVERING_TRACKING
            ),
            "3": _ups_notice("Your UPS Package was delivered", delivered_tracking),
        },
    )
    shipper = GenericShipper(hass, {"image_path": "test/path/"})

    result = await shipper.process_batch(
        account,
        "12-Aug-2026",
        ["ups_pickup", "ups_delivering", "ups_delivered"],
        None,
        since_date="05-Aug-2026",
    )

    assert result["ups_pickup"] == expected_count
    # The tracking_# attribute must not outlive the count it belongs to
    assert result["ups_pickup_tracking"] == expected_tracking
    # The rest of the batch is unaffected by the pickup deduplication
    assert result["ups_delivering"] == 1
    assert result["ups_delivered"] == 1
    # Pin the numbers each sibling sensor extracted, so a fixture that quietly
    # yields a different tracking number cannot fake the dedup result above
    assert result["_tracking_details"]["ups_delivering"] == [DELIVERING_TRACKING]
    assert result["_tracking_details"]["ups_delivered"] == [delivered_tracking]
    assert "ups_pickup" not in result["_tracking_details"]


@pytest.mark.asyncio
async def test_pickup_survives_its_own_still_open_delivering_notice(
    hass, mock_imap_mailbox
):
    """An out-for-delivery notice for the SAME parcel must not zero the pickup.

    This is the normal life of an Access Point parcel: UPS says it is out for
    delivery, fails to hand it over, and diverts it to the pickup point, so
    both emails carry one tracking number. The pickup sensor is deduplicated
    only against _delivered — i.e. against the notice UPS sends once the user
    has actually collected the parcel — never against the in-transit set. That
    is why it is registered as an update target and not as a package target:
    package targets are deduplicated against "delivering | delivered", which
    here would report 0 parcels waiting while one is demonstrably waiting.
    """
    account = mock_imap_mailbox(
        {
            "1": _recorded_pickup_notice(),
            "2": _ups_notice(
                "UPS Update: Package Scheduled for Delivery Today", PICKUP_TRACKING
            ),
            "3": _ups_notice("Your UPS Package was delivered", OTHER_TRACKING),
        },
    )
    shipper = GenericShipper(hass, {"image_path": "test/path/"})

    result = await shipper.process_batch(
        account,
        "12-Aug-2026",
        ["ups_pickup", "ups_delivering", "ups_delivered"],
        None,
        since_date="05-Aug-2026",
    )

    assert result["ups_pickup"] == 1
    assert result["ups_pickup_tracking"] == [PICKUP_TRACKING]
    # Documented consequence of leaving the in-transit transition to a
    # follow-up: the parcel is counted by _delivering AND _pickup until the
    # delivered notice for it arrives.
    assert result["ups_delivering"] == 1
    assert result["_tracking_details"]["ups_delivering"] == [PICKUP_TRACKING]


@pytest.mark.asyncio
async def test_pickup_does_not_subtract_from_packages(hass, mock_imap_mailbox):
    """A parcel waiting at an Access Point must still be counted by _packages.

    _packages is deduplicated against the shipper's shared "delivering" set so
    it only shows parcels not yet out for delivery or delivered. Adding pickup
    tracking numbers to that set would silently drop a parcel from _packages
    the moment it reached the Access Point, even though the ship notification
    is the only other email UPS sent about it — so the user would watch their
    package total fall by one while nothing had been delivered.
    """
    account = mock_imap_mailbox(
        {
            "1": _recorded_pickup_notice(),
            "2": _ups_notice("UPS Ship Notification", PICKUP_TRACKING),
        },
    )
    shipper = GenericShipper(hass, {"image_path": "test/path/"})

    result = await shipper.process_batch(
        account,
        "12-Aug-2026",
        ["ups_pickup", "ups_packages"],
        None,
        since_date="05-Aug-2026",
    )

    assert result["ups_packages"] == 1
    assert result["ups_packages_tracking"] == [PICKUP_TRACKING]
    assert result["ups_pickup"] == 1
    assert result["ups_pickup_tracking"] == [PICKUP_TRACKING]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("subject", "expected_sensor"),
    [
        # Both real ready-for-pickup subjects observed in the wild
        ("UPS - Package Ready for Pickup", "ups_pickup"),
        ("UPS My Choice - Package Ready for Pickup", "ups_pickup"),
        # Existing UPS subjects must keep mapping to their own sensor only
        ("Your UPS Package was delivered", "ups_delivered"),
        ("UPS Update: Package Scheduled for Delivery Today", "ups_delivering"),
        ("UPS Update: Follow Your Delivery on a Live Map", "ups_delivering"),
        ("UPS Update: New Scheduled Delivery Date", "ups_exception"),
        ("UPS Ship Notification", "ups_packages"),
    ],
)
async def test_ups_subject_patterns(hass, subject, expected_sensor):
    """Each real UPS subject is accepted by exactly one UPS sensor.

    The check runs through _verify_matched_subjects — the production matcher
    that decides which searched emails a sensor keeps — so a subject constant
    that only looks unambiguous cannot pass here.
    """
    shipper = GenericShipper(hass, {})
    mock_account = AsyncMock()

    with patch(
        "custom_components.mail_and_packages.shippers.generic.email_fetch_headers",
        return_value=("OK", [f"Subject: {subject}\r\n".encode()]),
    ):
        matched = [
            sensor
            for sensor in UPS_SUBJECT_SENSORS
            if await shipper._verify_matched_subjects(
                mock_account, [b"1"], sensor, SENSOR_DATA[sensor][ATTR_SUBJECT], None
            )
        ]

    assert matched == [expected_sensor]


@pytest.mark.asyncio
async def test_amazon_hub_subject_does_not_match_ups_pickup(hass):
    """The Amazon Hub locker phrase must not satisfy the UPS pickup subject.

    "Ready for Pickup" on its own is used by Amazon Hub lockers, restaurants
    and pharmacies alike, and the sender filter cannot be relied on to keep
    them out: on address-list forwarding GenericShipper._resolve_forwarding
    prepends the user's OWN forwarding addresses to the sender list, so for
    those users any relayed message satisfies the FROM clause. Hence the longer
    "Package Ready for Pickup" substring.
    """
    shipper = GenericShipper(hass, {})
    mock_account = AsyncMock()

    with patch(
        "custom_components.mail_and_packages.shippers.generic.email_fetch_headers",
        return_value=("OK", [f"Subject: {AMAZON_HUB_SUBJECT[0]}\r\n".encode()]),
    ):
        verified = await shipper._verify_matched_subjects(
            mock_account,
            [b"1"],
            "ups_pickup",
            SENSOR_DATA["ups_pickup"][ATTR_SUBJECT],
            None,
        )

    assert verified == []
