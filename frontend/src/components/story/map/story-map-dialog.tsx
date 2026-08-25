"use client";

import { useState } from "react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";

export type StoryMapDialogField = {
  name: string;
  label: string;
  defaultValue?: string;
  placeholder?: string;
  multiline?: boolean;
  required?: boolean;
};

export type StoryMapDialogRequest = {
  /** 同じ種類のダイアログを開き直したときに入力欄を初期化するためのキー。 */
  key: string;
  title: string;
  description?: string;
  confirmLabel: string;
  destructive?: boolean;
  fields?: StoryMapDialogField[];
  onConfirm: (values: Record<string, string>) => void | Promise<void>;
};

/** `window.prompt` / `window.confirm` の置き換え。入力欄なしなら確認ダイアログとして振る舞う。 */
export function StoryMapDialog({
  request,
  onClose,
}: {
  request: StoryMapDialogRequest | null;
  onClose: () => void;
}) {
  return (
    <Dialog
      open={Boolean(request)}
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
    >
      {request ? <StoryMapDialogBody key={request.key} request={request} onClose={onClose} /> : null}
    </Dialog>
  );
}

function StoryMapDialogBody({
  request,
  onClose,
}: {
  request: StoryMapDialogRequest;
  onClose: () => void;
}) {
  const fields = request.fields ?? [];
  const [values, setValues] = useState<Record<string, string>>(() =>
    Object.fromEntries(fields.map((field) => [field.name, field.defaultValue ?? ""])),
  );
  const [busy, setBusy] = useState(false);
  const incomplete = fields.some((field) => field.required && !(values[field.name] ?? "").trim());

  const submit = async () => {
    if (busy || incomplete) return;
    setBusy(true);
    try {
      await request.onConfirm(values);
      onClose();
    } finally {
      setBusy(false);
    }
  };

  return (
    <DialogContent size="md">
      <DialogHeader>
        <DialogTitle>{request.title}</DialogTitle>
        {request.description ? <DialogDescription>{request.description}</DialogDescription> : null}
      </DialogHeader>
      {fields.length ? (
        <div className="flex flex-col gap-3">
          {fields.map((field, index) => (
            <div key={field.name} className="flex flex-col gap-1.5">
              <Label htmlFor={`story-map-field-${field.name}`} className="text-xs text-muted-foreground">
                {field.label}
              </Label>
              {field.multiline ? (
                <Textarea
                  id={`story-map-field-${field.name}`}
                  rows={5}
                  autoFocus={index === 0}
                  placeholder={field.placeholder}
                  value={values[field.name] ?? ""}
                  onChange={(event) => setValues((current) => ({ ...current, [field.name]: event.target.value }))}
                />
              ) : (
                <Input
                  id={`story-map-field-${field.name}`}
                  autoFocus={index === 0}
                  placeholder={field.placeholder}
                  value={values[field.name] ?? ""}
                  onChange={(event) => setValues((current) => ({ ...current, [field.name]: event.target.value }))}
                  onKeyDown={(event) => {
                    if (event.key === "Enter") {
                      event.preventDefault();
                      void submit();
                    }
                  }}
                />
              )}
            </div>
          ))}
        </div>
      ) : null}
      <DialogFooter>
        <Button variant="outline" onClick={onClose} disabled={busy}>
          キャンセル
        </Button>
        <Button
          variant={request.destructive ? "destructive" : "default"}
          onClick={() => void submit()}
          disabled={busy || incomplete}
        >
          {busy ? "実行中…" : request.confirmLabel}
        </Button>
      </DialogFooter>
    </DialogContent>
  );
}
