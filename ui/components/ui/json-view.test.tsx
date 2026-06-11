import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render } from "@testing-library/react";
import { JsonView } from "./json-view";

afterEach(() => cleanup());

describe("JsonView", () => {
  it("serialises a value with indentation", () => {
    const { container } = render(
      <JsonView value={{ status: 403, ok: false, detail: "no" }} />,
    );
    const text = container.textContent ?? "";
    expect(text).toContain('"status"');
    expect(text).toContain("403");
    expect(text).toContain("false");
    expect(text).toContain('"detail"');
  });

  it("applies token classes on keys / strings / numbers / booleans", () => {
    const { container } = render(
      <JsonView value={{ k: "v", n: 42, b: true, z: null }} />,
    );
    expect(container.querySelector(".sg-json-k")).not.toBeNull();
    expect(container.querySelector(".sg-json-s")).not.toBeNull();
    expect(container.querySelector(".sg-json-n")).not.toBeNull();
    expect(container.querySelector(".sg-json-b")).not.toBeNull();
  });

  it("accepts raw pre-serialised JSON", () => {
    const { container } = render(
      <JsonView raw='{ "answer": 42 }' />,
    );
    expect(container.textContent).toContain("42");
  });

  it("emits comment lines", () => {
    const { container } = render(
      <JsonView
        value={{ a: 1 }}
        comments={{ before: "request", after: "response", afterValue: { b: 2 } }}
      />,
    );
    const text = container.textContent ?? "";
    expect(text).toContain("// request");
    expect(text).toContain("// response");
    expect(container.querySelectorAll(".sg-json-c").length).toBeGreaterThanOrEqual(2);
  });
});
