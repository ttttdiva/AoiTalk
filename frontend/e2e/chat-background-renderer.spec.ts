import { expect, test } from "@playwright/test";
import {
  CHAT_WAVE_ATLAS_HEIGHT,
  CHAT_WAVE_ATLAS_WIDTH,
  CHAT_WAVE_FRAGMENT_SHADER,
  CHAT_WAVE_GLYPH_ATLAS,
  CHAT_WAVE_VERTEX_SHADER,
  MAX_RIPPLES,
  RIPPLE_CONTOUR_SAMPLES,
  generateRippleContour,
  rippleSeed,
} from "../../resources/chat-background";

for (const dark of [false, true]) {
  for (const precision of ["highp", "mediump"] as const) {
    test(`idle drift stays seamless and twelve event contours vary (${dark ? "dark" : "light"}, ${precision})`, async ({
      page,
    }) => {
      const eventEntropies = [
        0.13, 0.19, 0.27, 0.34, 0.42, 0.51, 0.59, 0.66, 0.73, 0.81, 0.89, 0.97,
      ];
      const eventSeeds = eventEntropies.map((entropy) =>
        rippleSeed(192, 128, 1.5, entropy),
      );
      const result = await page.evaluate(
        ({
          vertex,
          fragment,
          atlas,
          atlasWidth,
          atlasHeight,
          dark,
          precision,
          contours,
          contourSamples,
          maxRipples,
          eventSeed,
          eventSeeds,
        }) => {
          const canvas = document.createElement("canvas");
          canvas.width = 384;
          canvas.height = 256;
          const gl = canvas.getContext("webgl", {
            preserveDrawingBuffer: true,
          });
          if (!gl) throw new Error("WebGL context unavailable");
          const compile = (type: number, source: string) => {
            const shader = gl.createShader(type)!;
            gl.shaderSource(shader, source);
            gl.compileShader(shader);
            if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS))
              throw new Error(
                gl.getShaderInfoLog(shader) || "Shader compile failed",
              );
            return shader;
          };
          const program = gl.createProgram()!;
          gl.attachShader(program, compile(gl.VERTEX_SHADER, vertex));
          gl.attachShader(
            program,
            compile(
              gl.FRAGMENT_SHADER,
              fragment.replace(
                /#ifdef GL_FRAGMENT_PRECISION_HIGH[\s\S]*?#endif/,
                `precision ${precision} float;`,
              ),
            ),
          );
          gl.linkProgram(program);
          if (!gl.getProgramParameter(program, gl.LINK_STATUS))
            throw new Error(
              gl.getProgramInfoLog(program) || "Program link failed",
            );
          gl.useProgram(program);
          const buffer = gl.createBuffer();
          gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
          gl.bufferData(
            gl.ARRAY_BUFFER,
            new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]),
            gl.STATIC_DRAW,
          );
          const position = gl.getAttribLocation(program, "a_position");
          gl.enableVertexAttribArray(position);
          gl.vertexAttribPointer(position, 2, gl.FLOAT, false, 0, 0);
          const texture = gl.createTexture();
          gl.bindTexture(gl.TEXTURE_2D, texture);
          gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
          gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
          gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
          gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
          const rgba = new Uint8Array(atlas.length * 4);
          atlas.forEach((value, index) =>
            rgba.set([value, value, value, 255], index * 4),
          );
          gl.texImage2D(
            gl.TEXTURE_2D,
            0,
            gl.RGBA,
            atlasWidth,
            atlasHeight,
            0,
            gl.RGBA,
            gl.UNSIGNED_BYTE,
            rgba,
          );
          gl.uniform1i(gl.getUniformLocation(program, "u_glyphAtlas"), 0);
          const contourTexture = gl.createTexture();
          gl.activeTexture(gl.TEXTURE1);
          gl.bindTexture(gl.TEXTURE_2D, contourTexture);
          gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
          gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
          gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.REPEAT);
          gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
          const contourAtlas = new Uint8Array(contourSamples * maxRipples * 4);
          for (let pixel = 0; pixel < contourSamples * maxRipples; pixel += 1) {
            contourAtlas.set([128, 0, 0, 255], pixel * 4);
          }
          gl.texImage2D(
            gl.TEXTURE_2D,
            0,
            gl.RGBA,
            contourSamples,
            maxRipples,
            0,
            gl.RGBA,
            gl.UNSIGNED_BYTE,
            contourAtlas,
          );
          gl.uniform1i(gl.getUniformLocation(program, "u_rippleContours"), 1);
          gl.activeTexture(gl.TEXTURE0);
          const configLocation = gl.getUniformLocation(program, "u_config");
          gl.uniform4f(configLocation, dark ? 1 : 0, 0.41731, 0, 0);
          gl.uniform2f(
            gl.getUniformLocation(program, "u_resolution"),
            canvas.width,
            canvas.height,
          );
          gl.uniform2f(
            gl.getUniformLocation(program, "u_drawResolution"),
            canvas.width,
            canvas.height,
          );
          const render = (driftX: number, driftY: number) => {
            // Freeze the independent oscillators to isolate each drift seam.
            gl.uniform4fv(
              gl.getUniformLocation(program, "u_motionPhases[0]"),
              new Float32Array([0.7, 1.1, 2.2, driftX, 1.3, driftY, 2.7, 0.4]),
            );
            gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
            const pixels = new Uint8Array(canvas.width * canvas.height * 4);
            gl.readPixels(
              0,
              0,
              canvas.width,
              canvas.height,
              gl.RGBA,
              gl.UNSIGNED_BYTE,
              pixels,
            );
            return pixels;
          };
          const reference = render(0, 0);
          const deltas = [
            [24, 0],
            [0, 24],
            [24, 24],
          ].map(([x, y]) => {
            const pixels = render(x, y);
            let sum = 0;
            let max = 0;
            pixels.forEach((value, index) => {
              const delta = Math.abs(value - reference[index]);
              sum += delta;
              max = Math.max(max, delta);
            });
            return { x, y, mean: sum / pixels.length, max };
          });
          const rowValues = [128, 224, 48, 80, 112, 144, 176, 208];
          const probeData = new Uint8Array(contourSamples * maxRipples * 4);
          for (let row = 0; row < maxRipples; row += 1) {
            for (let sample = 0; sample < contourSamples; sample += 1) {
              const red =
                row === 0 && sample === 0
                  ? 32
                  : row === 0 && sample === contourSamples - 1
                    ? 224
                    : rowValues[row];
              probeData.set(
                [red, 0, 0, 255],
                (row * contourSamples + sample) * 4,
              );
            }
          }
          gl.activeTexture(gl.TEXTURE1);
          gl.bindTexture(gl.TEXTURE_2D, contourTexture);
          gl.texSubImage2D(
            gl.TEXTURE_2D,
            0,
            0,
            0,
            contourSamples,
            maxRipples,
            gl.RGBA,
            gl.UNSIGNED_BYTE,
            probeData,
          );
          const probeProgram = gl.createProgram()!;
          gl.attachShader(probeProgram, compile(gl.VERTEX_SHADER, vertex));
          gl.attachShader(
            probeProgram,
            compile(
              gl.FRAGMENT_SHADER,
              `precision ${precision} float; uniform sampler2D u_contour; uniform vec2 u_uv; void main() { gl_FragColor = texture2D(u_contour, u_uv); }`,
            ),
          );
          gl.linkProgram(probeProgram);
          if (!gl.getProgramParameter(probeProgram, gl.LINK_STATUS)) {
            throw new Error(
              gl.getProgramInfoLog(probeProgram) ||
                "Contour lookup probe link failed",
            );
          }
          gl.useProgram(probeProgram);
          gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
          const probePosition = gl.getAttribLocation(
            probeProgram,
            "a_position",
          );
          gl.enableVertexAttribArray(probePosition);
          gl.vertexAttribPointer(probePosition, 2, gl.FLOAT, false, 0, 0);
          gl.uniform1i(gl.getUniformLocation(probeProgram, "u_contour"), 1);
          const uvLocation = gl.getUniformLocation(probeProgram, "u_uv");
          const readContour = (u: number, v: number) => {
            gl.uniform2f(uvLocation, u, v);
            gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
            const pixel = new Uint8Array(4);
            gl.readPixels(
              canvas.width / 2,
              canvas.height / 2,
              1,
              1,
              gl.RGBA,
              gl.UNSIGNED_BYTE,
              pixel,
            );
            return pixel[0];
          };
          const seamDelta = Math.abs(
            readContour(0, 0.5 / maxRipples) - readContour(1, 0.5 / maxRipples),
          );
          const rowCenterSamples = rowValues.map((_, row) =>
            readContour(0.25, (row + 0.5) / maxRipples),
          );
          gl.useProgram(null);
          gl.deleteProgram(probeProgram);
          gl.useProgram(program);
          gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
          gl.enableVertexAttribArray(position);
          gl.vertexAttribPointer(position, 2, gl.FLOAT, false, 0, 0);
          gl.activeTexture(gl.TEXTURE1);
          gl.bindTexture(gl.TEXTURE_2D, contourTexture);
          gl.activeTexture(gl.TEXTURE0);
          gl.uniform4f(configLocation, dark ? 1 : 0, 0.41731, 1, 0);
          const eventFrames = contours.map((contour) => {
            for (let sample = 0; sample < contourSamples; sample += 1) {
              contourAtlas[sample * 4] = contour[sample];
            }
            gl.activeTexture(gl.TEXTURE1);
            gl.bindTexture(gl.TEXTURE_2D, contourTexture);
            gl.texSubImage2D(
              gl.TEXTURE_2D,
              0,
              0,
              0,
              contourSamples,
              maxRipples,
              gl.RGBA,
              gl.UNSIGNED_BYTE,
              contourAtlas,
            );
            gl.uniform4fv(
              gl.getUniformLocation(program, "u_ripples[0]"),
              new Float32Array([192, 128, 1.6, eventSeed]),
            );
            gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
            const pixels = new Uint8Array(canvas.width * canvas.height * 4);
            gl.readPixels(
              0,
              0,
              canvas.width,
              canvas.height,
              gl.RGBA,
              gl.UNSIGNED_BYTE,
              pixels,
            );
            return pixels;
          });
          const eventPairMeans: number[] = [];
          for (let first = 0; first < eventFrames.length; first += 1) {
            for (
              let second = first + 1;
              second < eventFrames.length;
              second += 1
            ) {
              let sum = 0;
              eventFrames[first].forEach((value, index) => {
                sum += Math.abs(value - eventFrames[second][index]);
              });
              eventPairMeans.push(sum / eventFrames[first].length);
            }
          }
          const neutralContour = new Array(contourSamples).fill(128);
          const dynamicFrames = eventSeeds.map((seed) => {
            for (let sample = 0; sample < contourSamples; sample += 1) {
              contourAtlas[sample * 4] = neutralContour[sample];
            }
            gl.activeTexture(gl.TEXTURE1);
            gl.bindTexture(gl.TEXTURE_2D, contourTexture);
            gl.texSubImage2D(
              gl.TEXTURE_2D,
              0,
              0,
              0,
              contourSamples,
              maxRipples,
              gl.RGBA,
              gl.UNSIGNED_BYTE,
              contourAtlas,
            );
            gl.uniform4fv(
              gl.getUniformLocation(program, "u_ripples[0]"),
              new Float32Array([192, 128, 1.6, seed]),
            );
            gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
            const pixels = new Uint8Array(canvas.width * canvas.height * 4);
            gl.readPixels(
              0,
              0,
              canvas.width,
              canvas.height,
              gl.RGBA,
              gl.UNSIGNED_BYTE,
              pixels,
            );
            return pixels;
          });
          const dynamicPairMeans: number[] = [];
          for (let first = 0; first < dynamicFrames.length; first += 1) {
            for (
              let second = first + 1;
              second < dynamicFrames.length;
              second += 1
            ) {
              let sum = 0;
              dynamicFrames[first].forEach((value, index) => {
                sum += Math.abs(value - dynamicFrames[second][index]);
              });
              dynamicPairMeans.push(sum / dynamicFrames[first].length);
            }
          }
          const error = gl.getError();
          const activeUniforms = Array.from(
            {
              length: gl.getProgramParameter(
                program,
                gl.ACTIVE_UNIFORMS,
              ) as number,
            },
            (_, index) => gl.getActiveUniform(program, index),
          ).filter((uniform): uniform is WebGLActiveInfo => uniform !== null);
          const usedUniformVectors = activeUniforms.reduce(
            (total, uniform) => total + uniform.size,
            0,
          );
          const fragmentTextureUnits = gl.getParameter(
            gl.MAX_TEXTURE_IMAGE_UNITS,
          ) as number;
          gl.getExtension("WEBGL_lose_context")?.loseContext();
          return {
            deltas,
            eventPairMeans,
            dynamicPairMeans,
            error,
            usedUniformVectors,
            fragmentTextureUnits,
            contourLookup: {
              seamDelta,
              rowCenterSamples,
              expectedRows: rowValues,
            },
          };
        },
        {
          vertex: CHAT_WAVE_VERTEX_SHADER,
          fragment: CHAT_WAVE_FRAGMENT_SHADER,
          atlas: Array.from(CHAT_WAVE_GLYPH_ATLAS.data),
          atlasWidth: CHAT_WAVE_ATLAS_WIDTH,
          atlasHeight: CHAT_WAVE_ATLAS_HEIGHT,
          dark,
          precision,
          contourSamples: RIPPLE_CONTOUR_SAMPLES,
          maxRipples: MAX_RIPPLES,
          eventSeed: rippleSeed(192, 128, 1.5, 0.37),
          eventSeeds,
          contours: eventSeeds.map((seed) =>
            Array.from(generateRippleContour(seed)),
          ),
        },
      );
      await test.info().attach("ripple-event-pixels", {
        body: JSON.stringify(result),
        contentType: "application/json",
      });
      expect(result.error).toBe(0);
      expect(result.usedUniformVectors).toBe(15);
      expect(result.fragmentTextureUnits).toBeGreaterThanOrEqual(2);
      expect(result.contourLookup.seamDelta).toBe(0);
      expect(result.contourLookup.rowCenterSamples).toEqual(
        result.contourLookup.expectedRows,
      );
      for (const delta of result.deltas) {
        // Allow low-precision rounding; a wrapped field must not change its
        // spatial pattern. The prior fractional drift produced a visible jump.
        expect(delta.mean, JSON.stringify(delta)).toBeLessThan(0.05);
      }
      expect(result.eventPairMeans.length).toBe(66);
      // Keep the ripple seed and age fixed to isolate the event contour from
      // speed, follower timing, brightness, and time-dependent noise.
      expect(Math.min(...result.eventPairMeans)).toBeGreaterThan(0.15);
      expect(result.dynamicPairMeans.length).toBe(66);
      // Keep the contour circular here so this measurement covers event scale
      // and follower parameters independently of silhouette variation.
      expect(Math.min(...result.dynamicPairMeans)).toBeGreaterThan(0.12);
    });
  }
}
