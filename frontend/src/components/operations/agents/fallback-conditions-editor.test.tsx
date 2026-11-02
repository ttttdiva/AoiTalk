// @vitest-environment jsdom

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { FALLBACK_CONDITION_CODES, FallbackConditionsEditor } from "./fallback-conditions-editor";

describe("FallbackConditionsEditor", () => {
  it("controls default, explicit and disabled values from props", async () => {
    const user = userEvent.setup(); const change = vi.fn();
    const view = render(<FallbackConditionsEditor value={null} onChange={change} idPrefix="test" />);
    const defaults = screen.getByRole("checkbox", { name: "既定の技術エラー条件を使う" });
    expect(defaults).toBeChecked(); expect(screen.getByRole("checkbox", { name: "レート制限に達した" })).toHaveAttribute("aria-disabled", "true");
    await user.click(defaults); expect(change).toHaveBeenLastCalledWith([...FALLBACK_CONDITION_CODES]);
    view.rerender(<FallbackConditionsEditor value={["connect_timeout"]} onChange={change} idPrefix="test" />);
    await user.click(screen.getByRole("checkbox", { name: "モデルが利用できない" }));
    expect(change).toHaveBeenLastCalledWith(["model_unavailable", "connect_timeout"]);
    await user.click(screen.getByRole("checkbox", { name: "接続がタイムアウトした" })); expect(change).toHaveBeenLastCalledWith([]);
    await user.click(defaults); expect(change).toHaveBeenLastCalledWith(null);
    view.rerender(<FallbackConditionsEditor value={[]} onChange={change} idPrefix="test" disabled />);
    expect(defaults).toHaveAttribute("aria-disabled", "true"); expect(screen.getByRole("checkbox", { name: "モデルが利用できない" })).not.toBeChecked();
    change.mockClear(); await user.click(defaults); expect(change).not.toHaveBeenCalled();
  });
});
