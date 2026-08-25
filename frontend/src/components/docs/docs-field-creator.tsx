"use client";

import { AppSelect } from "@/components/ui/app-select";

import {
  useState,
} from "react";
import {
  Plus,
} from "lucide-react";
import {
  Button,
} from "@/components/ui/button";
import {
  Input,
} from "@/components/ui/input";

// SupertagConfigPanel からフィールドを新規追加する入力UI。
export function FieldCreator({ tagId, onCreateField }: { tagId: string; onCreateField: (tagId: string, name: string, fieldType: string) => void }) {
  const [name, setName] = useState("");
  const [fieldType, setFieldType] = useState("text");
  return (
    <div className="mt-2 grid grid-cols-[minmax(0,1fr)_96px_auto] gap-1">
      <Input value={name} onChange={(event) => setName(event.target.value)} placeholder="New field" className="h-7 text-xs" />
      <AppSelect value={fieldType} onChange={(event) => setFieldType(event.target.value)} className="h-7 rounded border bg-background px-1 text-xs">
        {["text", "options", "date", "checkbox", "reference", "number"].map((type) => (
          <option key={type} value={type}>
            {type}
          </option>
        ))}
      </AppSelect>
      <Button
        type="button"
        size="icon-sm"
        variant="ghost"
        onClick={() => {
          onCreateField(tagId, name, fieldType);
          setName("");
        }}
      >
        <Plus className="size-4" />
      </Button>
    </div>
  );
}
