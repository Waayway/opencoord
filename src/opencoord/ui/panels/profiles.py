"""Profiles tab: device profile editor and spacing preset editor (two sub-tabs).

All behaviour is in :class:`opencoord.ui.profiles_actions.ProfilesActions` and the drafts of
:mod:`opencoord.ui.profile_editor`; this panel renders them and forwards edits. Widget callbacks
only change the draft. ``update()`` (once per frame, after the callbacks ran) rebuilds the row
widgets when the draft's structure changed (rows added or removed, another draft) and otherwise
refreshes texts and buttons when a version counter moved, so typing is never interrupted.
"""

from __future__ import annotations

from collections.abc import Callable

import dearpygui.dearpygui as dpg

from opencoord.ui import theme
from opencoord.ui.profile_editor import (
    MODE_LABELS,
    MODES,
    SPACING_FIELDS,
    SPACING_LABELS,
    ChannelList,
    PresetDraft,
    ProfileDraft,
    format_khz,
    friendly,
)
from opencoord.ui.profiles_actions import ProfilesActions

#: ``ask(title, extensions, action, default_name=...)``: a file dialog (see ``FileUI.ask``).
AskFile = Callable[..., None]

WARN_COLOR = (255, 190, 80, 255)
WRAP = 340
MAX_ISSUES_SHOWN = 8
MAX_ERRORS_SHOWN = 6
TEMPLATE_PLACEHOLDER = "New from template..."

#: Tags of the rebuilt widgets (their cached texts and settings are dropped on a rebuild).
_DYNAMIC_PREFIXES = ("profiles.range", "profiles.channels", "profiles.group", "profiles.step")

TAG_MESSAGE = "profiles.message"
TAG_ISSUES = "profiles.issues"
TAG_LIST = "profiles.list"
TAG_TEMPLATES = "profiles.templates"
TAG_CONFIRM = "profiles.confirm"
TAG_CONFIRM_TEXT = "profiles.confirm.text"
TAG_CONFIRM_YES = "profiles.confirm.yes"
TAG_EDITOR = "profiles.editor"
TAG_HEADER = "profiles.header"
TAG_NAME = "profiles.name"
TAG_KIND = "profiles.kind"
TAG_MODE = "profiles.mode"
TAG_MODE_WARNING = "profiles.mode_warning"
TAG_SOURCE = "profiles.source"
TAG_PREVIEW = "profiles.preview"
TAG_PRESET = "profiles.preset"
TAG_ERRORS = "profiles.errors"
TAG_SAVE = "profiles.save"
TAG_CLONE = "profiles.clone"
TAG_EXPORT = "profiles.export"
TAG_DELETE = "profiles.delete"
TAG_CHANNELS = "profiles.channels"
TAG_STEP = "profiles.step"

TAG_PLIST = "presets.list"
TAG_PMISSING = "presets.missing"
TAG_PCONFIRM = "presets.confirm"
TAG_PCONFIRM_TEXT = "presets.confirm.text"
TAG_PCONFIRM_YES = "presets.confirm.yes"
TAG_PEDITOR = "presets.editor"
TAG_PHEADER = "presets.header"
TAG_PNAME = "presets.name"
TAG_PDESC = "presets.description"
TAG_PUSED = "presets.used_by"
TAG_PERRORS = "presets.errors"
TAG_PSAVE = "presets.save"
TAG_PCLONE = "presets.clone"
TAG_PRESET_RESET = "presets.reset"
TAG_PDELETE = "presets.delete"


def override_tag(name: str, part: str) -> str:
    return f"profiles.override.{name}.{part}"


def preset_value_tag(name: str) -> str:
    return f"presets.value.{name}"


def header_text(kind: str, name: str, unsaved: bool, is_new: bool) -> str:
    """``Editing profile: Name`` plus the unsaved-changes marker."""
    marker = " [not saved yet]" if is_new else " [unsaved changes]" if unsaved else ""
    return f"Editing {kind}: {name.strip() or '(no name)'}{marker}"


def list_info(channels: ChannelList) -> str:
    """One line under a channel box: the count and what was merged, or that it has bad entries."""
    n = len(channels.values)
    text = f"{n} channel{'s' if n != 1 else ''}"
    if channels.duplicates:
        text += f", {channels.duplicates} duplicate{'s' if channels.duplicates != 1 else ''} merged"
    bad = len(channels.errors)
    if bad:
        text += f"; {bad} {'entries' if bad != 1 else 'entry'} not understood"
    return text


def errors_text(errors: list[str] | tuple[str, ...]) -> str:
    shown = [f"- {e}" for e in errors[:MAX_ERRORS_SHOWN]]
    if len(errors) > MAX_ERRORS_SHOWN:
        shown.append(f"- and {len(errors) - MAX_ERRORS_SHOWN} more")
    return "\n".join(shown)


class ProfilesPanel:
    def __init__(self, actions: ProfilesActions, ask: AskFile) -> None:
        self._a = actions
        self._ask = ask
        self._text: dict[str, str] = {}
        self._configured: dict[str, str] = {}
        self._static_inputs: list[str] = []
        self._dynamic_inputs: list[str] = []
        self._sync_key: tuple[int, int] | None = None
        self._psync_key: int | None = None
        self._seen: tuple[object, ...] = ()
        self._issues_key: object = None
        self._missing_key: object = None
        self._templates = {label: key for key, label in actions.template_choices()}

    # --- helpers ---------------------------------------------------------------------------

    @property
    def text_inputs(self) -> list[str]:
        return list(self._static_inputs)

    def is_typing(self) -> bool:
        """A text or number field has the keyboard (shortcuts must stay quiet)."""
        return any(
            dpg.does_item_exist(tag) and dpg.is_item_active(tag)
            for tag in (*self._static_inputs, *self._dynamic_inputs)
        )

    def _set(self, tag: str, text: str) -> None:
        if self._text.get(tag) != text:
            self._text[tag] = text
            dpg.set_value(tag, text)

    def _cfg(self, tag: str, **kwargs: object) -> None:
        key = repr(sorted(kwargs.items()))
        if self._configured.get(tag) != key:
            self._configured[tag] = key
            dpg.configure_item(tag, **kwargs)

    def _value(self, tag: str, value: object) -> None:
        """Set a widget value unless the user is editing it or it already shows ``value``."""
        if dpg.get_value(tag) != value and not dpg.is_item_active(tag):
            dpg.set_value(tag, value)

    def _static_input(self, tag: str) -> str:
        self._static_inputs.append(tag)
        return tag

    # --- profile callbacks -------------------------------------------------------------------

    def _draft(self) -> ProfileDraft | None:
        return self._a.draft

    def _on_name(self, _s: object, value: str) -> None:
        if (d := self._draft()) is not None:
            d.set_name(value)

    def _on_kind(self, _s: object, value: str) -> None:
        if (d := self._draft()) is not None:
            d.set_kind(value)

    def _on_mode(self, _s: object, label: str) -> None:
        if (d := self._draft()) is not None:
            d.set_mode(next(m for m in MODES if MODE_LABELS[m] == label))

    def _on_preset(self, _s: object, value: str) -> None:
        if (d := self._draft()) is not None:
            d.set_preset(value)

    def _on_range(self, _s: object, _v: object, index: int) -> None:
        if (d := self._draft()) is not None and index < len(d.ranges):
            d.set_range(
                index,
                float(dpg.get_value(f"profiles.range.{index}.start")),
                float(dpg.get_value(f"profiles.range.{index}.stop")),
            )

    def _on_remove_range(self, _s: object, _v: object, index: int) -> None:
        if (d := self._draft()) is not None and index < len(d.ranges):
            d.remove_range(index)

    def _on_step(self, _s: object, value: float) -> None:
        if (d := self._draft()) is not None:
            d.set_step_khz(float(value))

    def _on_channels(self, _s: object, value: str, group: int | None) -> None:
        if (d := self._draft()) is not None:
            d.paste_channels(value, group)

    def _on_tidy(self, _s: object, _v: object, group: int | None) -> None:
        d = self._draft()
        if d is None:
            return
        if d.tidy_channels(group):
            self._sync_key = None  # show the cleaned-up text
        else:
            self._a.say("Fix the entries that are not understood first", error=True)

    def _on_group_name(self, _s: object, value: str, index: int) -> None:
        if (d := self._draft()) is not None and index < len(d.groups):
            d.rename_group(index, value)

    def _on_remove_group(self, _s: object, _v: object, index: int) -> None:
        if (d := self._draft()) is not None and index < len(d.groups):
            d.remove_group(index)

    def _on_override_check(self, _s: object, checked: bool, name: str) -> None:
        d = self._draft()
        if d is None:
            return
        if checked:
            base = self._preset_khz(d.preset, name)
            d.set_override(name, base if base is not None else 0.0)
            dpg.set_value(override_tag(name, "value"), d.overrides[name])
        else:
            d.clear_override(name)

    def _on_override_value(self, _s: object, value: float, name: str) -> None:
        if (d := self._draft()) is not None and d.overrides.get(name) is not None:
            d.set_override(name, float(value))

    def _preset_khz(self, preset: str, name: str) -> float | None:
        p = self._a.presets.get(preset)
        return None if p is None else getattr(p.rules, name) / 1000

    def _on_pick_profile(self, _s: object, name: str) -> None:
        self._a.select_profile(name)

    def _on_template(self, _s: object, label: str) -> None:
        key = self._templates.get(label)
        dpg.set_value(TAG_TEMPLATES, TEMPLATE_PLACEHOLDER)
        if key is not None:
            self._a.new_from_template(key)

    def _import(self) -> None:
        self._ask("Import profile", [".toml", ".*"], self._a.import_profile)

    def _export(self) -> None:
        self._ask(
            "Export profile",
            [".toml"],
            self._a.export_profile,
            default_name=self._a.default_export_name(),
        )

    # --- preset callbacks --------------------------------------------------------------------

    def _on_pname(self, _s: object, value: str) -> None:
        if self._a.preset_draft is not None:
            self._a.preset_draft.set_name(value)

    def _on_pdesc(self, _s: object, value: str) -> None:
        if self._a.preset_draft is not None:
            self._a.preset_draft.set_description(value)

    def _on_pvalue(self, _s: object, value: float, name: str) -> None:
        if self._a.preset_draft is not None:
            self._a.preset_draft.set_value(name, float(value))

    def _on_pick_preset(self, _s: object, name: str) -> None:
        self._a.select_preset(name)

    def _reset_preset(self) -> None:
        self._a.request_reset_preset()

    # --- layout ------------------------------------------------------------------------------

    def build(self) -> None:
        dpg.add_group(tag=TAG_ISSUES)
        dpg.add_text("", tag=TAG_MESSAGE, wrap=WRAP)
        with dpg.tab_bar(tag="profiles.sub"):
            with dpg.tab(label="Profiles", tag="profiles.sub.profiles"):
                self._build_profiles()
            with dpg.tab(label="Spacing presets", tag="profiles.sub.presets"):
                self._build_presets()

    def _confirm_bar(self, tag: str, text_tag: str, yes_tag: str) -> None:
        with dpg.group(tag=tag, show=False):
            dpg.add_text("", tag=text_tag, wrap=WRAP, color=WARN_COLOR)
            with dpg.group(horizontal=True):
                dpg.add_button(label="Confirm", tag=yes_tag, callback=self._a.confirm_pending)
                dpg.add_button(label="Cancel", callback=self._a.cancel_pending)

    def _build_profiles(self) -> None:
        a = self._a
        dpg.add_combo([], tag=TAG_LIST, label="Open", width=190, callback=self._on_pick_profile)
        with dpg.group(horizontal=True):
            dpg.add_button(label="New blank", callback=lambda: a.new_blank())
            dpg.add_button(label="Import...", callback=self._import)
        dpg.add_combo(
            list(self._templates),
            tag=TAG_TEMPLATES,
            default_value=TEMPLATE_PLACEHOLDER,
            width=-1,
            callback=self._on_template,
        )
        self._confirm_bar(TAG_CONFIRM, TAG_CONFIRM_TEXT, TAG_CONFIRM_YES)
        dpg.add_separator()
        with dpg.group(tag=TAG_EDITOR, show=False):
            dpg.add_text("", tag=TAG_HEADER, wrap=WRAP)
            with dpg.group(horizontal=True):
                dpg.add_button(label="Save", tag=TAG_SAVE, callback=lambda: a.save_profile())
                dpg.add_button(label="Clone", tag=TAG_CLONE, callback=lambda: a.clone_profile())
                dpg.add_button(label="Export...", tag=TAG_EXPORT, callback=self._export)
                dpg.add_button(
                    label="Delete", tag=TAG_DELETE, callback=lambda: a.request_delete_profile()
                )
            dpg.add_text("", tag=TAG_ERRORS, wrap=WRAP, color=theme.ERROR_COLOR)
            dpg.add_input_text(
                tag=self._static_input(TAG_NAME), label="Name", width=-80, callback=self._on_name
            )
            dpg.add_combo(
                ["mic", "iem", "other"],
                tag=TAG_KIND,
                label="Kind",
                width=-80,
                callback=self._on_kind,
            )
            dpg.add_text("Frequencies come from")
            dpg.add_radio_button(
                [MODE_LABELS[m] for m in MODES], tag=TAG_MODE, callback=self._on_mode
            )
            dpg.add_text("", tag=TAG_MODE_WARNING, wrap=WRAP, color=WARN_COLOR)
            dpg.add_group(tag=TAG_SOURCE)
            dpg.add_text("", tag=TAG_PREVIEW, wrap=WRAP, color=theme.OK_COLOR)
            dpg.add_separator()
            dpg.add_text("Spacing")
            dpg.add_combo([], tag=TAG_PRESET, label="Preset", width=-80, callback=self._on_preset)
            self._build_override_table()

    def _build_override_table(self) -> None:
        with dpg.table(
            header_row=True,
            borders_innerH=True,
            policy=dpg.mvTable_SizingStretchProp,
            tag="profiles.overrides",
        ):
            dpg.add_table_column(label="Rule", init_width_or_weight=1.3)
            dpg.add_table_column(label="Used", init_width_or_weight=0.9)
            dpg.add_table_column(label="Override (kHz)", init_width_or_weight=1.4)
            for name in SPACING_FIELDS:
                with dpg.table_row():
                    dpg.add_text(SPACING_LABELS[name], wrap=120)
                    dpg.add_text("", tag=override_tag(name, "effective"), wrap=80)
                    with dpg.group(horizontal=True):
                        dpg.add_checkbox(
                            tag=override_tag(name, "check"),
                            user_data=name,
                            callback=self._on_override_check,
                        )
                        dpg.add_input_double(
                            tag=self._static_input(override_tag(name, "value")),
                            width=70,
                            step=0,
                            format="%.3f",
                            min_value=0.0,
                            min_clamped=True,
                            enabled=False,
                            user_data=name,
                            callback=self._on_override_value,
                        )
        dpg.add_text(
            "Used = preset value, or your override. 0 turns a rule off. Tick a box to override "
            "that rule for this profile only.",
            color=theme.MUTED_COLOR,
            wrap=WRAP,
        )

    def _build_presets(self) -> None:
        a = self._a
        dpg.add_combo([], tag=TAG_PLIST, label="Open", width=190, callback=self._on_pick_preset)
        with dpg.group(horizontal=True):
            dpg.add_button(label="New preset", callback=lambda: a.new_preset())
        dpg.add_group(tag=TAG_PMISSING)
        self._confirm_bar(TAG_PCONFIRM, TAG_PCONFIRM_TEXT, TAG_PCONFIRM_YES)
        dpg.add_separator()
        with dpg.group(tag=TAG_PEDITOR, show=False):
            dpg.add_text("", tag=TAG_PHEADER, wrap=WRAP)
            with dpg.group(horizontal=True):
                dpg.add_button(label="Save", tag=TAG_PSAVE, callback=lambda: a.save_preset())
                dpg.add_button(label="Clone", tag=TAG_PCLONE, callback=lambda: a.clone_preset())
                dpg.add_button(
                    label="Delete", tag=TAG_PDELETE, callback=lambda: a.request_delete_preset()
                )
            dpg.add_button(
                label="Reset to built-in values",
                tag=TAG_PRESET_RESET,
                width=-1,
                callback=self._reset_preset,
            )
            dpg.add_text("", tag=TAG_PERRORS, wrap=WRAP, color=theme.ERROR_COLOR)
            dpg.add_text("", tag=TAG_PUSED, wrap=WRAP, color=theme.MUTED_COLOR)
            dpg.add_input_text(
                tag=self._static_input(TAG_PNAME), label="Name", width=-80, callback=self._on_pname
            )
            dpg.add_input_text(
                tag=self._static_input(TAG_PDESC),
                label="Note",
                width=-80,
                callback=self._on_pdesc,
            )
            dpg.add_text("Minimum spacing in kHz (0 = rule off)")
            for name in SPACING_FIELDS:
                dpg.add_input_double(
                    tag=self._static_input(preset_value_tag(name)),
                    label=SPACING_LABELS[name],
                    width=90,
                    step=0,
                    format="%.3f",
                    user_data=name,
                    callback=self._on_pvalue,
                )
            dpg.add_text(
                "Spacing values are starting points, not guarantees: check them against your "
                "devices' data sheets.",
                color=theme.MUTED_COLOR,
                wrap=WRAP,
            )

    # --- dynamic rows ------------------------------------------------------------------------

    def _rebuild_source(self, d: ProfileDraft) -> None:
        dpg.delete_item(TAG_SOURCE, children_only=True)
        self._dynamic_inputs = []
        for cache in (self._text, self._configured):
            for tag in [t for t in cache if t.startswith(_DYNAMIC_PREFIXES)]:
                del cache[tag]
        if d.mode == "tuning":
            self._build_tuning(d)
        elif d.mode == "channels":
            dpg.add_text(
                "One MHz value per line, or separated by semicolons, tabs, spaces or a comma and "
                "a space. Decimals: 470.125 or 470,125. Duplicates are merged and the list is "
                "sorted.",
                color=theme.MUTED_COLOR,
                wrap=WRAP,
                parent=TAG_SOURCE,
            )
            self._build_list(d.channels, None, TAG_CHANNELS)
        else:
            self._build_groups(d)

    def _dyn_input(self, tag: str) -> str:
        self._dynamic_inputs.append(tag)
        return tag

    def _build_tuning(self, d: ProfileDraft) -> None:
        dpg.add_text("Tuning ranges (MHz)", parent=TAG_SOURCE)
        for i, r in enumerate(d.ranges):
            with dpg.group(horizontal=True, parent=TAG_SOURCE):
                for part, value in (("start", r.start_mhz), ("stop", r.stop_mhz)):
                    dpg.add_input_double(
                        tag=self._dyn_input(f"profiles.range.{i}.{part}"),
                        default_value=value,
                        width=100,
                        step=0,
                        format="%.4f",
                        user_data=i,
                        callback=self._on_range,
                    )
                dpg.add_button(
                    label="Remove",
                    user_data=i,
                    callback=self._on_remove_range,
                    tag=f"profiles.range.{i}.remove",
                )
        dpg.add_button(
            label="Add range", tag="profiles.range.add", parent=TAG_SOURCE, callback=self._add_range
        )
        dpg.add_input_double(
            tag=self._dyn_input(TAG_STEP),
            label="Step (kHz)",
            default_value=d.step_khz,
            width=100,
            step=0,
            format="%.3f",
            parent=TAG_SOURCE,
            callback=self._on_step,
        )

    def _add_range(self) -> None:
        if (d := self._draft()) is not None:
            d.add_range()

    def _add_group(self) -> None:
        if (d := self._draft()) is not None:
            d.add_group()

    def _build_list(self, channels: ChannelList, group: int | None, tag: str) -> None:
        dpg.add_input_text(
            tag=self._dyn_input(tag),
            default_value=channels.text,
            multiline=True,
            height=110,
            width=-1,
            parent=TAG_SOURCE,
            user_data=group,
            callback=self._on_channels,
        )
        with dpg.group(horizontal=True, parent=TAG_SOURCE):
            dpg.add_text(list_info(channels), tag=f"{tag}.info")
            dpg.add_button(
                label="Clean up", tag=f"{tag}.tidy", user_data=group, callback=self._on_tidy
            )

    def _build_groups(self, d: ProfileDraft) -> None:
        dpg.add_text(
            "Each group is a bank or block of channels. Paste its MHz values below.",
            color=theme.MUTED_COLOR,
            wrap=WRAP,
            parent=TAG_SOURCE,
        )
        for i, g in enumerate(d.groups):
            with dpg.group(horizontal=True, parent=TAG_SOURCE):
                dpg.add_input_text(
                    tag=self._dyn_input(f"profiles.group.{i}.name"),
                    default_value=g.name,
                    width=200,
                    user_data=i,
                    callback=self._on_group_name,
                )
                dpg.add_button(
                    label="Remove group",
                    user_data=i,
                    callback=self._on_remove_group,
                    tag=f"profiles.group.{i}.remove",
                )
            self._build_list(g.channels, i, f"profiles.group.{i}.channels")
        dpg.add_button(
            label="Add group", tag="profiles.group.add", parent=TAG_SOURCE, callback=self._add_group
        )

    def _rebuild_missing(self) -> None:
        dpg.delete_item(TAG_PMISSING, children_only=True)
        for name in self._a.missing_builtin_presets():
            dpg.add_button(
                label=f"Restore built-in '{name}'",
                parent=TAG_PMISSING,
                user_data=name,
                callback=lambda _s, _v, n: self._a.reset_preset_to_builtin(n),
            )

    def _rebuild_issues(self) -> None:
        dpg.delete_item(TAG_ISSUES, children_only=True)
        issues = self._a.issues
        if not issues:
            return
        n = len(issues)
        dpg.add_text(
            f"{n} file{'s' if n != 1 else ''} could not be loaded:",
            color=theme.ERROR_COLOR,
            parent=TAG_ISSUES,
        )
        for issue in issues[:MAX_ISSUES_SHOWN]:
            dpg.add_text(
                f"{issue.path.name}: {friendly(issue.message)}",
                color=theme.ERROR_COLOR,
                wrap=WRAP,
                bullet=True,
                parent=TAG_ISSUES,
            )
        if n > MAX_ISSUES_SHOWN:
            dpg.add_text(f"... and {n - MAX_ISSUES_SHOWN} more", parent=TAG_ISSUES)
        dpg.add_separator(parent=TAG_ISSUES)

    # --- per frame ---------------------------------------------------------------------------

    def update(self) -> None:
        a = self._a
        d, pd = a.draft, a.preset_draft
        seen = (a.version, d.uid if d else 0, d.revision if d else 0)
        pseen = (pd.uid if pd else 0, pd.revision if pd else 0)
        if (*seen, *pseen) == self._seen:
            return
        self._seen = (*seen, *pseen)
        self._update_common()
        self._update_profiles(d)
        self._update_presets(pd)

    def _update_common(self) -> None:
        a = self._a
        key = tuple((str(i.path), i.message) for i in a.issues)
        if key != self._issues_key:
            self._issues_key = key
            self._rebuild_issues()
        self._set(TAG_MESSAGE, a.message)
        self._cfg(TAG_MESSAGE, color=theme.ERROR_COLOR if a.message_is_error else theme.OK_COLOR)
        missing = tuple(a.missing_builtin_presets())
        if missing != self._missing_key:
            self._missing_key = missing
            self._rebuild_missing()

    def _update_profiles(self, d: ProfileDraft | None) -> None:
        a = self._a
        self._cfg(TAG_LIST, items=a.profile_names)
        shown = d.original_name if d is not None and d.original_name else ""
        self._value(TAG_LIST, shown)
        pending = a.pending if a.pending is not None and a.pending.scope == "profile" else None
        self._cfg(TAG_CONFIRM, show=pending is not None)
        if pending is not None:
            self._set(TAG_CONFIRM_TEXT, pending.prompt)
            self._cfg(TAG_CONFIRM_YES, label=pending.confirm_label)
        self._cfg(TAG_EDITOR, show=d is not None)
        if d is None:
            return
        key = (d.uid, d.structure_version)
        if key != self._sync_key:
            self._sync_key = key
            self._rebuild_source(d)
            self._sync_fixed_widgets(d)
        status = a.profile_status()
        self._set(TAG_HEADER, header_text("profile", d.name, d.dirty, d.is_new))
        self._set(TAG_MODE_WARNING, d.dropped_warning() or "")
        self._cfg(TAG_PRESET, items=a.preset_names)
        self._value(TAG_PRESET, d.preset)
        self._update_list_infos(d)
        if status.preview is not None:
            lines = [status.preview.text]
            if status.preview.per_group:
                lines.append(", ".join(f"{n}: {c}" for n, c in status.preview.per_group))
            self._set(TAG_PREVIEW, "\n".join(lines))
        else:
            self._set(TAG_PREVIEW, "Preview appears when the profile is valid")
        self._cfg(
            TAG_PREVIEW,
            color=theme.OK_COLOR if status.preview is not None else theme.MUTED_COLOR,
        )
        self._update_overrides(d)
        self._set(TAG_ERRORS, errors_text(status.errors))
        self._cfg(TAG_SAVE, enabled=status.profile is not None and d.unsaved)
        self._cfg(TAG_CLONE, enabled=True)
        self._cfg(TAG_EXPORT, enabled=status.profile is not None)
        self._cfg(TAG_DELETE, label="Discard" if d.is_new else "Delete")

    def _sync_fixed_widgets(self, d: ProfileDraft) -> None:
        dpg.set_value(TAG_NAME, d.name)
        dpg.set_value(TAG_KIND, d.kind)
        dpg.set_value(TAG_MODE, MODE_LABELS[d.mode])
        dpg.set_value(TAG_PRESET, d.preset)
        for name in SPACING_FIELDS:
            value = d.overrides[name]
            dpg.set_value(override_tag(name, "check"), value is not None)
            dpg.set_value(override_tag(name, "value"), value if value is not None else 0.0)

    def _update_list_infos(self, d: ProfileDraft) -> None:
        if d.mode == "channels":
            self._set(f"{TAG_CHANNELS}.info", list_info(d.channels))
        elif d.mode == "groups":
            for i, g in enumerate(d.groups):
                self._set(f"profiles.group.{i}.channels.info", list_info(g.channels))

    def _update_overrides(self, d: ProfileDraft) -> None:
        preset = self._a.presets.get(d.preset)
        for row in d.effective_spacing(preset.rules if preset else None):
            eff = "?" if row.effective_khz is None else f"{format_khz(row.effective_khz)} kHz"
            if row.overridden:
                eff += " (override)"
            self._set(override_tag(row.name, "effective"), eff)
            on = d.overrides[row.name] is not None
            self._cfg(override_tag(row.name, "value"), enabled=on)
            if dpg.get_value(override_tag(row.name, "check")) != on:
                dpg.set_value(override_tag(row.name, "check"), on)
            khz = d.overrides[row.name]
            if khz is not None:
                self._value(override_tag(row.name, "value"), khz)

    def _update_presets(self, d: PresetDraft | None) -> None:
        a = self._a
        self._cfg(TAG_PLIST, items=a.preset_names)
        self._value(TAG_PLIST, d.original_name if d is not None and d.original_name else "")
        pending = a.pending if a.pending is not None and a.pending.scope == "preset" else None
        self._cfg(TAG_PCONFIRM, show=pending is not None)
        if pending is not None:
            self._set(TAG_PCONFIRM_TEXT, pending.prompt)
            self._cfg(TAG_PCONFIRM_YES, label=pending.confirm_label)
        self._cfg(TAG_PEDITOR, show=d is not None)
        if d is None:
            return
        if d.uid != self._psync_key:
            self._psync_key = d.uid
            dpg.set_value(TAG_PNAME, d.name)
            dpg.set_value(TAG_PDESC, d.description)
            for name in SPACING_FIELDS:
                dpg.set_value(preset_value_tag(name), d.values[name])
        errors = a.preset_errors()
        self._set(TAG_PHEADER, header_text("spacing preset", d.name, d.dirty, d.is_new))
        self._set(TAG_PERRORS, errors_text(errors))
        users = a.profiles_using(d.original_name) if d.original_name else []
        self._set(
            TAG_PUSED,
            f"Used by {len(users)} profile{'s' if len(users) != 1 else ''}: " + ", ".join(users[:5])
            if users
            else "Not used by any profile",
        )
        self._cfg(TAG_PSAVE, enabled=not errors and d.unsaved)
        self._cfg(TAG_PDELETE, label="Discard" if d.is_new else "Delete")
        builtin = d.original_name is not None and a.is_builtin_preset(d.original_name)
        self._cfg(TAG_PRESET_RESET, show=builtin)
        self._cfg(TAG_PCLONE, enabled=True)


__all__ = ["ProfilesPanel", "errors_text", "header_text", "list_info"]
