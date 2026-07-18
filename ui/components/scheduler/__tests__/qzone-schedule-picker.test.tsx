import * as React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

import { QzoneSchedulePicker } from "@/components/scheduler/qzone-schedule-picker";
import type { ScheduleState } from "@/lib/cron-schedule";

// Return the raw key so assertions don't depend on the translated copy.
vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (k: string) => k }),
}));

afterEach(cleanup);

function state(over: Partial<ScheduleState> = {}): ScheduleState {
  return { mode: "daily", time: "09:00", weekdays: [], raw: "0 9 * * *", ...over };
}

/** Controlled harness so chip clicks actually flow back into `value`. */
function Harness({
  initial,
  onState,
}: {
  initial: ScheduleState;
  onState?: (s: ScheduleState) => void;
}) {
  const [s, setS] = React.useState(initial);
  return (
    <QzoneSchedulePicker
      value={s}
      onChange={(next) => {
        setS(next);
        onState?.(next);
      }}
    />
  );
}

const K = "schedulerQzone.schedule";

describe("QzoneSchedulePicker", () => {
  it("renders the controls that belong to each mode", () => {
    const { rerender } = render(
      <QzoneSchedulePicker value={state({ mode: "daily" })} onChange={() => {}} />,
    );
    // daily: time only
    expect(screen.getByTestId("qzone-schedule-time")).toBeInTheDocument();
    expect(screen.queryByTestId("qzone-schedule-raw")).toBeNull();
    expect(screen.queryByText(`${K}.dowMon`)).toBeNull();

    // weekly: time + weekday chips
    rerender(
      <QzoneSchedulePicker value={state({ mode: "weekly" })} onChange={() => {}} />,
    );
    expect(screen.getByTestId("qzone-schedule-time")).toBeInTheDocument();
    expect(screen.getByText(`${K}.dowMon`)).toBeInTheDocument();
    expect(screen.getByText(`${K}.dowSun`)).toBeInTheDocument();

    // advanced: raw cron only
    rerender(
      <QzoneSchedulePicker value={state({ mode: "advanced" })} onChange={() => {}} />,
    );
    expect(screen.getByTestId("qzone-schedule-raw")).toBeInTheDocument();
    expect(screen.queryByTestId("qzone-schedule-time")).toBeNull();
  });

  it("switches mode without dropping time / weekdays / raw", () => {
    const onChange = vi.fn();
    render(
      <QzoneSchedulePicker
        value={state({ mode: "daily", time: "07:15", weekdays: [3], raw: "5 5 * * 2" })}
        onChange={onChange}
      />,
    );
    fireEvent.click(screen.getByText(`${K}.modeWeekly`));
    expect(onChange).toHaveBeenCalledWith({
      mode: "weekly",
      time: "07:15",
      weekdays: [3],
      raw: "5 5 * * 2",
    });
  });

  it("maps weekday chips to canonical 0..6 (Sunday = 0)", () => {
    const onState = vi.fn();
    render(<Harness initial={state({ mode: "weekly", weekdays: [] })} onState={onState} />);
    fireEvent.click(screen.getByText(`${K}.dowMon`)); // → 1
    fireEvent.click(screen.getByText(`${K}.dowSun`)); // → 0
    const last = onState.mock.calls.at(-1)?.[0] as ScheduleState;
    expect(last.weekdays).toEqual(expect.arrayContaining([0, 1]));
    expect(last.weekdays).toHaveLength(2);
  });

  it("emits time edits in daily mode", () => {
    const onChange = vi.fn();
    render(
      <QzoneSchedulePicker value={state({ mode: "daily" })} onChange={onChange} />,
    );
    fireEvent.change(screen.getByTestId("qzone-schedule-time"), {
      target: { value: "21:30" },
    });
    expect(onChange).toHaveBeenCalledWith(
      expect.objectContaining({ mode: "daily", time: "21:30" }),
    );
  });

  it("emits raw cron edits in advanced mode", () => {
    const onChange = vi.fn();
    render(
      <QzoneSchedulePicker value={state({ mode: "advanced" })} onChange={onChange} />,
    );
    fireEvent.change(screen.getByTestId("qzone-schedule-raw"), {
      target: { value: "*/5 * * * *" },
    });
    expect(onChange).toHaveBeenCalledWith(
      expect.objectContaining({ mode: "advanced", raw: "*/5 * * * *" }),
    );
  });
});
