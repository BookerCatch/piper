# SPDX-License-Identifier: GPL-2.0-or-later

"""A battery indicator for the device shown in Piper.

ratbagd does not report battery levels; the kernel does, and UPower publishes
them on the system bus. This widget finds the UPower device that belongs to
the ratbagd device and shows its charge as an icon and a percentage.
"""

import os
import re
from gettext import gettext as _
from typing import Any, List, Optional, Tuple

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gio, GLib, Gtk  # noqa

UPOWER_NAME = "org.freedesktop.UPower"
UPOWER_PATH = "/org/freedesktop/UPower"
UPOWER_DEVICE_INTERFACE = "org.freedesktop.UPower.Device"

# How long to wait for UPower before giving up, in milliseconds.
DBUS_TIMEOUT_MS = 1000

# Kernel bus numbers for the bus names ratbagd uses in its model string.
BUS_IDS = {"usb": 0x0003, "bluetooth": 0x0005}

# A HID device directory in sysfs, e.g. "0003:046D:4099.000B".
HID_DEVICE_RE = re.compile(
    r"([0-9A-Fa-f]{4}):([0-9A-Fa-f]{4}):([0-9A-Fa-f]{4})\.[0-9A-Fa-f]+"
)


def N_(text: str) -> str:
    """Marks a string for translation without translating it yet."""
    return text


# UPower's org.freedesktop.UPower.Device.State values.
STATE_LABELS = {
    1: N_("Charging"),
    2: N_("Discharging"),
    3: N_("Empty"),
    4: N_("Fully charged"),
    5: N_("Not charging"),
    6: N_("Discharging"),
}
STATE_CHARGING = 1
STATE_FULLY_CHARGED = 4

HidId = Tuple[int, int, int]


def parse_ratbag_model(model: str) -> Optional[HidId]:
    """Turns a ratbagd model string such as "usb:046d:4099:0" into
    (bus, vendor, product), or None if it isn't in that form."""
    parts = model.split(":")
    if len(parts) < 3 or parts[0] not in BUS_IDS:
        return None
    try:
        return (BUS_IDS[parts[0]], int(parts[1], 16), int(parts[2], 16))
    except ValueError:
        return None


def hid_id_for_power_supply(native_path: str) -> Optional[HidId]:
    """Returns (bus, vendor, product) of the HID device that owns a kernel
    power supply, such as "hidpp_battery_0", by following its sysfs link."""
    if not native_path or "/" in native_path:
        return None
    device_link = os.path.join("/sys/class/power_supply", native_path, "device")
    real_path = os.path.realpath(device_link)
    for part in reversed(real_path.split("/")):
        match = HID_DEVICE_RE.fullmatch(part)
        if match:
            bus, vendor, product = (int(group, 16) for group in match.groups())
            return (bus, vendor, product)
    return None


def icon_name_for(percentage: int, state: int) -> str:
    """Picks a symbolic battery icon when UPower doesn't provide one."""
    if percentage >= 90:
        level = "full"
    elif percentage >= 50:
        level = "good"
    elif percentage >= 20:
        level = "low"
    elif percentage >= 5:
        level = "caution"
    else:
        level = "empty"
    if state == STATE_FULLY_CHARGED:
        return "battery-full-charged-symbolic"
    if state == STATE_CHARGING:
        return f"battery-{level}-charging-symbolic"
    return f"battery-{level}-symbolic"


def _cached(proxy: Gio.DBusProxy, name: str, default: Any) -> Any:
    value = proxy.get_cached_property(name)
    return default if value is None else value.unpack()


class BatteryIndicator(Gtk.Box):
    """Shows a device's battery charge as an icon and a percentage.

    Stays hidden when UPower isn't running or reports no battery for the
    device, so wired mice look the same as before.
    """

    __gtype_name__ = "BatteryIndicator"

    def __init__(self, *args, **kwargs) -> None:
        Gtk.Box.__init__(
            self, *args, orientation=Gtk.Orientation.HORIZONTAL, spacing=4, **kwargs
        )
        self._image = Gtk.Image.new_from_icon_name(
            "battery-missing-symbolic", Gtk.IconSize.BUTTON
        )
        self._label = Gtk.Label()
        self.pack_start(self._image, False, False, 0)
        self.pack_start(self._label, False, False, 0)
        self._image.show()
        self._label.show()
        # Only show this once a battery is found, even when the parent calls
        # show_all().
        self.set_no_show_all(True)

        self._model: Optional[HidId] = None
        self._name = ""
        self._battery: Optional[Gio.DBusProxy] = None
        self._battery_handler = 0
        self._upower: Optional[Gio.DBusProxy] = None
        self._upower_handler = 0

        try:
            self._upower = Gio.DBusProxy.new_for_bus_sync(
                Gio.BusType.SYSTEM,
                Gio.DBusProxyFlags.DO_NOT_LOAD_PROPERTIES,
                None,
                UPOWER_NAME,
                UPOWER_PATH,
                UPOWER_NAME,
                None,
            )
        except GLib.Error:
            self._upower = None
        else:
            self._upower_handler = self._upower.connect(
                "g-signal", self._on_upower_signal
            )

        self.connect("destroy", self._on_destroy)

    def set_device(self, name: str, model: str) -> None:
        """Looks up the battery for the ratbagd device with this name and model
        string."""
        self._name = name or ""
        self._model = parse_ratbag_model(model or "")
        self._find_battery()

    def _find_battery(self) -> None:
        found = self._lookup_battery()
        current = self._battery
        if (
            found is not None
            and current is not None
            and found.get_object_path() == current.get_object_path()
        ):
            # Same battery as before: keep the existing proxy and signal.
            return
        self._set_battery(found)

    def _lookup_battery(self) -> Optional[Gio.DBusProxy]:
        if self._upower is None or not (self._name or self._model):
            return None
        try:
            result = self._upower.call_sync(
                "EnumerateDevices",
                None,
                Gio.DBusCallFlags.NONE,
                DBUS_TIMEOUT_MS,
                None,
            )
        except GLib.Error:
            return None

        (paths,) = result.unpack()
        by_name: List[Gio.DBusProxy] = []
        for path in paths:
            try:
                proxy = Gio.DBusProxy.new_for_bus_sync(
                    Gio.BusType.SYSTEM,
                    Gio.DBusProxyFlags.NONE,
                    None,
                    UPOWER_NAME,
                    path,
                    UPOWER_DEVICE_INTERFACE,
                    None,
                )
            except GLib.Error:
                continue

            # The most reliable match: the battery belongs to the same
            # USB or Bluetooth device as the one ratbagd is configuring.
            native_path = _cached(proxy, "NativePath", "")
            if self._model is not None and (
                hid_id_for_power_supply(native_path) == self._model
            ):
                return proxy

            model_name = _cached(proxy, "Model", "")
            if self._name and model_name.casefold() == self._name.casefold():
                by_name.append(proxy)

        # Fall back to the device name, but only when it's unambiguous.
        if len(by_name) == 1:
            return by_name[0]
        return None

    def _set_battery(self, proxy: Optional[Gio.DBusProxy]) -> None:
        if self._battery is not None and self._battery_handler:
            self._battery.disconnect(self._battery_handler)
        self._battery = proxy
        self._battery_handler = 0
        if proxy is not None:
            self._battery_handler = proxy.connect(
                "g-properties-changed", self._on_battery_properties_changed
            )
        self._update()

    def _update(self) -> None:
        battery = self._battery
        if battery is None or not _cached(battery, "IsPresent", False):
            self.hide()
            return

        percentage = int(round(_cached(battery, "Percentage", 0.0)))
        state = _cached(battery, "State", 0)
        icon_name = _cached(battery, "IconName", "") or icon_name_for(percentage, state)

        self._image.set_from_icon_name(icon_name, Gtk.IconSize.BUTTON)
        self._label.set_label(f"{percentage}%")

        tooltip = _("Battery: {percentage}%").format(percentage=percentage)
        state_label = STATE_LABELS.get(state)
        if state_label is not None:
            tooltip = f"{tooltip} · {_(state_label)}"
        self.set_tooltip_text(tooltip)
        self.show()

    def _on_battery_properties_changed(
        self, _proxy: Gio.DBusProxy, _changed: GLib.Variant, _invalidated: List[str]
    ) -> None:
        self._update()

    def _on_upower_signal(
        self,
        _proxy: Gio.DBusProxy,
        _sender: Optional[str],
        signal_name: str,
        _parameters: GLib.Variant,
    ) -> None:
        # A wireless mouse's battery appears when it wakes up or reconnects,
        # and disappears when it is switched off.
        if signal_name in ("DeviceAdded", "DeviceRemoved"):
            self._find_battery()

    def _on_destroy(self, _widget: Gtk.Widget) -> None:
        self._set_battery(None)
        if self._upower is not None and self._upower_handler:
            self._upower.disconnect(self._upower_handler)
            self._upower_handler = 0
