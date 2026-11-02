import { createElement } from "react";
import { createRoot } from "react-dom/client";
import { AppDownloadLink } from "@/components/apps/app-download-link";
import { explorerDownloadPaths } from "@/lib/explorer-api";
import {
  readFileTransferCompatibilityPreference,
  saveFileTransferCompatibilityPreference,
} from "@/lib/file-transfer-preference";

const fixtureFilename = "transfer-fixture.txt";
const fixturePath = "_projects/project-transfer-fixture/transfer-fixture.txt";
const fixtureAppId = "2e66ac62-6a90-4b8e-bcca-b7c50bfe10c9";
const appDownloadUrl = `/api/python-proxy/apps/${fixtureAppId}/files/download?path=${encodeURIComponent(fixtureFilename)}`;

const modeLabel = document.querySelector<HTMLElement>("[data-mode-label]");
const statusLabel = document.querySelector<HTMLElement>(
  "[data-download-status]",
);

function updateModeLabel() {
  if (modeLabel) {
    modeLabel.textContent = readFileTransferCompatibilityPreference()
      ? "ON"
      : "OFF";
  }
}

async function reportDownload(label: string, action: () => Promise<unknown>) {
  if (statusLabel) statusLabel.textContent = `${label}: downloading`;
  try {
    await action();
    if (statusLabel) statusLabel.textContent = `${label}: complete`;
  } catch (error) {
    if (statusLabel) {
      statusLabel.textContent = `${label}: ${error instanceof Error ? error.message : "download failed"}`;
    }
  }
}

document
  .querySelector<HTMLButtonElement>("[data-toggle-mode]")
  ?.addEventListener("click", () => {
    saveFileTransferCompatibilityPreference(
      !readFileTransferCompatibilityPreference(),
    );
    updateModeLabel();
  });

document
  .querySelector<HTMLButtonElement>("[data-files-download]")
  ?.addEventListener("click", () => {
    void reportDownload("Files", () => explorerDownloadPaths([fixturePath]));
  });

const appsDownloadRoot =
  document.querySelector<HTMLElement>("[data-apps-root]");
if (appsDownloadRoot) {
  createRoot(appsDownloadRoot).render(
    createElement(
      AppDownloadLink,
      {
        href: appDownloadUrl,
        fallbackFilename: fixtureFilename,
        download: true,
      },
      "Apps fixture download",
    ),
  );
}

updateModeLabel();
