// @vitest-environment jsdom

import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { EmployeeDirectory } from "./employee-directory";
import type { Employee } from "@/lib/agent-employees-api";

const agents: Employee[] = [
  { id: "agent-a", slug: "research", display_name: "調査担当", state: "active" },
  { id: "agent-b", slug: "supplies", display_name: "備品担当", state: "draft" },
];
const projections = [{ id: "agent-a", job_title: "資料整理" }];
const props = { agents, projections, selected: "agent-a", loading: false, failed: false, onSelect: vi.fn() };

describe("Agent一覧での検索と選択", () => {
  it("only shows a role when it adds information beyond the normalized name", () => {
    const duplicateAgents: Employee[] = [
      { ...agents[0], display_name: "SNS共有調査担当" },
      { ...agents[1], display_name: "ＯＦＦＢＹ１ ブランド担当" },
      { ...agents[0], id: "agent-c" },
      { ...agents[1], id: "agent-d" },
    ];
    render(<EmployeeDirectory {...props} agents={duplicateAgents} projections={[
      { id: "agent-a", job_title: "SNS共有調査担当" },
      { id: "agent-b", job_title: " offby1 ブランド担当 " },
      { id: "agent-c", job_title: "資料整理" },
    ]} />);
    const rows = within(screen.getByRole("region", { name: "Agentの検索結果" })).getAllByRole("button");
    expect(within(rows[0]).getAllByText("SNS共有調査担当")).toHaveLength(1);
    expect(within(rows[1]).queryByText("offby1 ブランド担当")).toBeNull();
    expect(within(rows[2]).getByText("資料整理")).toBeVisible();
    expect(screen.queryByText("役割未設定")).toBeNull();
  });

  it("searches normalized names, roles, slugs and IDs without changing selection", async () => {
    const user = userEvent.setup();
    render(<EmployeeDirectory {...props} />);
    const search = screen.getByRole("searchbox", { name: "Agentを検索" });
    const results = screen.getByRole("region", { name: "Agentの検索結果" });
    for (const term of ["調査", "資料整理", "ＲＥＳＥＡＲＣＨ", "agent-a"]) {
      await user.clear(search); await user.type(search, term);
      expect(within(results).getAllByRole("button")).toHaveLength(1);
      expect(within(results).getByRole("button", { pressed: true })).toHaveTextContent("調査担当");
    }
    await user.clear(search); await user.type(search, "見つからない");
    expect(screen.getByText("一致するAgentはありません。")).toBeVisible();
    expect(props.onSelect).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "検索をクリア" }));
    expect(within(results).getAllByRole("button")).toHaveLength(2);
    expect(within(results).getByRole("button", { pressed: true })).toHaveTextContent("調査担当");
  });

  it("restores the browse position after clearing a search", () => {
    render(<EmployeeDirectory {...props} />);
    const results = screen.getByRole("region", { name: "Agentの検索結果" });
    results.scrollTop = 640;
    fireEvent.scroll(results);
    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "備品" } });
    results.scrollTop = 0;
    fireEvent.scroll(results);
    fireEvent.click(screen.getByRole("button", { name: "検索をクリア" }));
    expect(results.scrollTop).toBe(640);
  });

  it("keeps search, selection and browsing position while the gallery is hidden", () => {
    const { rerender } = render(<EmployeeDirectory {...props} />);
    const results = screen.getByRole("region", { name: "Agentの検索結果" });
    const search = screen.getByRole("searchbox");
    let visible = true;
    Object.defineProperty(results, "clientHeight", { get: () => visible ? 400 : 0 });
    results.scrollTop = 640; fireEvent.scroll(results);
    fireEvent.change(search, { target: { value: "調査" } });
    visible = false;
    rerender(<EmployeeDirectory {...props} visible={false} />);
    results.scrollTop = 0; fireEvent.scroll(results);
    visible = true;
    rerender(<EmployeeDirectory {...props} visible />);
    expect(screen.getByRole("searchbox")).toBe(search);
    expect(search).toHaveValue("調査");
    expect(results.scrollTop).toBe(640);
    expect(within(results).getByRole("button", { pressed: true })).toHaveTextContent("調査担当");
  });

  it("distinguishes loading, an empty directory, failure and an empty search", () => {
    const { rerender } = render(<EmployeeDirectory {...props} agents={[]} loading />);
    expect(screen.getByText("Agentを読み込み中…")).toBeVisible();
    expect(screen.queryByText("Agentはまだ登録されていません。")).toBeNull();
    rerender(<EmployeeDirectory {...props} agents={[]} failed />);
    expect(screen.queryByText("Agentはまだ登録されていません。")).toBeNull();
    rerender(<EmployeeDirectory {...props} agents={[]} />);
    expect(screen.getByText("Agentはまだ登録されていません。")).toBeVisible();
  });
});
