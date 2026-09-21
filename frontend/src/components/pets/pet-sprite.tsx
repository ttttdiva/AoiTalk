"use client";
import { useEffect, useRef, useState } from "react";
import { ATLAS, type PetMotion } from "@/lib/pets/codex-pet";
import { mountPetRenderer } from "@/lib/pets/pet-renderer";

export function PetSprite({ image, motion = "idle", look = 0, reduced = false, size = 128, label }: {
  image: Blob; motion?: PetMotion; look?: number; reduced?: boolean; size?: number; label: string;
}) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const engine = useRef<ReturnType<typeof mountPetRenderer> | null>(null);
  const config = useRef({ motion, look, reduced });
  const [failedImage, setFailedImage] = useState<Blob | null>(null);
  useEffect(() => {
    config.current = { motion, look, reduced };
    engine.current?.update(config.current);
  }, [motion, look, reduced]);
  useEffect(() => {
    if (!canvas.current) return;
    const renderer = mountPetRenderer(canvas.current, image, () => setFailedImage(image));
    engine.current = renderer; renderer.update(config.current);
    return () => { renderer.dispose(); engine.current = null; };
  }, [image]);
  if (failedImage === image) return <span role="alert" className="text-xs text-destructive">ペット画像を表示できません。設定から再インポートしてください。</span>;
  return <canvas ref={canvas} width={ATLAS.cellWidth} height={ATLAS.cellHeight} role="img" aria-label={label}
    style={{ width: size, height: size * ATLAS.cellHeight / ATLAS.cellWidth, imageRendering: "pixelated", display: "block" }} />;
}
