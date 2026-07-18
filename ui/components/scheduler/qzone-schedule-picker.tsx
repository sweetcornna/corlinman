"use client";

/**
 * `<QzoneSchedulePicker>` — controlled (dumb) trigger-time picker for the
 * QZone daily-publish scheduler form.
 *
 * Instead of hand-typing a crontab, operators pick one of three modes and
 * the parent turns the resulting {@link ScheduleState} into a cron string
 * via `composeCron` (see `lib/cron-schedule.ts`). This component owns no
 * state of its own — `value` in, `onChange` out — so switching modes never
 * drops the fields the other modes care about (`time` / `weekdays` / `raw`
 * all ride along in a single `ScheduleState`).
 *
 *   daily     — one `HH:MM` time; fires every day.
 *   weekly    — the same time + a Monday-first weekday multi-select.
 *   advanced  — a raw 5-field cron string (the escape hatch).
 *
 * Design note (mirrors `cron-schedule.ts`): weekday chip *values* are the
 * canonical `0=Sunday .. 6=Saturday`, but they render Monday-first
 * (一二三四五六日) to match how zh operators read a week.
 */

import * as React from "react";
import { useTranslation } from "react-i18next";

import { FilterChipGroup } from "@/components/ui/filter-chip-group";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import type { ScheduleMode, ScheduleState } from "@/lib/cron-schedule";

export interface QzoneSchedulePickerProps {
  /** Current picker state — the parent is the single source of truth. */
  value: ScheduleState;
  /** Emits the next state on every edit; the parent re-renders with it. */
  onChange: (next: ScheduleState) => void;
}

/** Weekday chips render Monday-first for display; the stored values stay
 * `0=Sun .. 6=Sat` so `composeCron` sees the canonical set. */
const WEEKDAY_ORDER = [1, 2, 3, 4, 5, 6, 0] as const;

/** i18n key suffix per weekday value (0=Sun .. 6=Sat). */
const WEEKDAY_KEY: Record<number, string> = {
  0: "dowSun",
  1: "dowMon",
  2: "dowTue",
  3: "dowWed",
  4: "dowThu",
  5: "dowFri",
  6: "dowSat",
};

export function QzoneSchedulePicker({ value, onChange }: QzoneSchedulePickerProps) {
  const { t } = useTranslation();

  const modeOptions = [
    { value: "daily", label: t("schedulerQzone.schedule.modeDaily") },
    { value: "weekly", label: t("schedulerQzone.schedule.modeWeekly") },
    { value: "advanced", label: t("schedulerQzone.schedule.modeAdvanced") },
  ];

  const weekdayOptions = WEEKDAY_ORDER.map((d) => ({
    value: String(d),
    label: t(`schedulerQzone.schedule.${WEEKDAY_KEY[d]}`),
  }));

  return (
    <div className="flex flex-col gap-3" data-testid="qzone-schedule-picker">
      <FilterChipGroup
        options={modeOptions}
        value={value.mode}
        onChange={(next) => onChange({ ...value, mode: next as ScheduleMode })}
        label={t("schedulerQzone.fieldCron")}
      />

      {value.mode !== "advanced" ? (
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="qzone-schedule-time">
            {t("schedulerQzone.schedule.timeLabel")}
          </Label>
          <Input
            id="qzone-schedule-time"
            type="time"
            value={value.time}
            onChange={(e) => onChange({ ...value, time: e.target.value })}
            className="max-w-[160px]"
            data-testid="qzone-schedule-time"
          />
        </div>
      ) : null}

      {value.mode === "weekly" ? (
        <div className="flex flex-col gap-1.5">
          <Label>{t("schedulerQzone.schedule.weekdaysLabel")}</Label>
          <FilterChipGroup
            multi
            options={weekdayOptions}
            value={value.weekdays.map(String)}
            onChange={(next) =>
              onChange({
                ...value,
                weekdays: next.map((v) => Number.parseInt(v, 10)),
              })
            }
            label={t("schedulerQzone.schedule.weekdaysLabel")}
          />
        </div>
      ) : null}

      {value.mode === "advanced" ? (
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="qzone-schedule-raw">
            {t("schedulerQzone.schedule.rawLabel")}
          </Label>
          <Input
            id="qzone-schedule-raw"
            type="text"
            value={value.raw}
            onChange={(e) => onChange({ ...value, raw: e.target.value })}
            className="max-w-[260px] font-mono"
            placeholder="0 9 * * *"
            spellCheck={false}
            data-testid="qzone-schedule-raw"
          />
        </div>
      ) : null}
    </div>
  );
}
