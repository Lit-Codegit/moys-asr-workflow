// Dev-only Playwright regression for the ASS subtitle preview (决策 43).
// Runs against BOTH the localhost editor server and the portable blank-editor.html.
// The old global preview box (preview.subtitle normalized geometry) is REMOVED;
// this spec now proves the replacement semantics:
//   - overlay renders at the style default position (an2 bottom-center)
//   - dragging the overlay writes a per-segment overrides.pos in PlayRes coords
//     without touching any segment timing/text
//   - a drag gesture is a single undo step, redo re-applies it
//   - double-click opens the style panel; applying a field writes an override
//   - the style manager opens from the toolbar button
//   - pos persists through server save+reload and portable export+reimport
import { expect, test } from '@playwright/test';
import { join } from 'node:path';
import { readFileSync } from 'node:fs';
import {
  cleanupTempDir,
  DURATION_MS,
  findFreePort,
  generateBlankEditor,
  generateProjectJson,
  generateWav,
  makeTempDir,
  startServer,
  startStaticServer,
  testSegments,
} from './helpers.mjs';

let tempDir;
let projectPath;
let server;

const EXPECTED_SEGMENTS = testSegments();

test.beforeAll(async () => {
  tempDir = makeTempDir('preview-geometry');
  const mediaPath = join(tempDir, 'synthetic.wav');
  projectPath = join(tempDir, 'project.json');
  generateWav(mediaPath, DURATION_MS / 1000);
  generateProjectJson(projectPath);
  server = await startServer(projectPath, mediaPath, await findFreePort());
});

test.afterAll(async () => {
  await server?.stop();
  cleanupTempDir(tempDir);
});

// Move the playhead into segment 0 so the overlay text is shown.
async function revealOverlay(page) {
  await page.evaluate(() => {
    const media = document.getElementById('player');
    media.currentTime = 1;
    media.dispatchEvent(new Event('seeked'));
    media.dispatchEvent(new Event('timeupdate'));
  });
  const overlay = page.locator('#overlay');
  await expect(overlay).toBeVisible();
  return overlay;
}

function readSegments(page) {
  return page.evaluate(() => DATA.segments.map((s) => ({
    start: s.start,
    end: s.end,
    text: s.text,
    items: (s.items || []).map((i) => ({ start: i.start, end: i.end, text: i.text })),
    overrides: s.overrides || null,
    style_ref: s.style_ref || null,
  })));
}

function onDiskSegmentTiming(segments) {
  return segments.map((s) => ({
    start: s.start,
    end: s.end,
    text: s.text,
    items: (s.items || []).map((i) => ({ start: i.start, end: i.end, text: i.text })),
  }));
}

test('overlay renders at the style default without writing any segment pos', async ({ page }) => {
  await page.goto(server.url);
  const overlay = await revealOverlay(page);
  await expect(overlay.locator('span')).toHaveClass(/ass-styled/);
  const segs = await readSegments(page);
  expect(segs[0].overrides).toBeNull();
  expect(segs).toEqual(EXPECTED_SEGMENTS.map((s) => ({ ...s, overrides: null, style_ref: null })));
});

test('dragging the overlay writes a per-segment PlayRes pos without touching timing', async ({ page }) => {
  await page.goto(server.url);
  const overlay = await revealOverlay(page);

  const box = await overlay.boundingBox();
  expect(box).not.toBeNull();
  await page.mouse.move(box.x + box.width * 0.5, box.y + box.height * 0.5);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width * 0.5 - 60, box.y + box.height * 0.5 - 120, { steps: 8 });
  await page.mouse.up();

  const segs = await readSegments(page);
  const pos = segs[0].overrides?.pos;
  expect(Array.isArray(pos)).toBe(true);
  expect(Number.isInteger(pos[0])).toBe(true);
  expect(Number.isInteger(pos[1])).toBe(true);
  // 只写 pos，不动时间轴/文本；其他段无覆盖
  expect(segs[0].start).toBe(EXPECTED_SEGMENTS[0].start);
  expect(segs[0].end).toBe(EXPECTED_SEGMENTS[0].end);
  expect(segs[0].text).toBe(EXPECTED_SEGMENTS[0].text);
  expect(segs.slice(1).every((s) => s.overrides === null)).toBe(true);
  // PlayRes 范围内
  const playres = await page.evaluate(() => {
    const p = document.getElementById('player');
    return [p.videoWidth || 1280, p.videoHeight || 720];
  });
  expect(pos[0]).toBeGreaterThanOrEqual(0);
  expect(pos[0]).toBeLessThanOrEqual(playres[0]);
  expect(pos[1]).toBeGreaterThanOrEqual(0);
  expect(pos[1]).toBeLessThanOrEqual(playres[1]);
});

test('a drag gesture is a single undo step and redo re-applies it', async ({ page }) => {
  await page.goto(server.url);
  const overlay = await revealOverlay(page);

  const box = await overlay.boundingBox();
  await page.mouse.move(box.x + box.width * 0.5, box.y + box.height * 0.5);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width * 0.5 - 40, box.y + box.height * 0.5 - 100, { steps: 6 });
  await page.mouse.up();
  const moved = (await readSegments(page))[0].overrides?.pos;
  expect(moved).toBeTruthy();

  const undo = page.getByRole('button', { name: /撤销/ });
  const redo = page.getByRole('button', { name: /重做/ });
  await expect(undo).toBeEnabled();

  await undo.click();
  expect((await readSegments(page))[0].overrides).toBeNull();

  await expect(redo).toBeEnabled();
  await redo.click();
  expect((await readSegments(page))[0].overrides?.pos).toEqual(moved);
});

test('double-click opens the style panel and applying a field writes an override', async ({ page }) => {
  await page.goto(server.url);
  const overlay = await revealOverlay(page);
  const box = await overlay.boundingBox();
  await page.mouse.dblclick(box.x + box.width * 0.5, box.y + box.height * 0.5);
  const panel = page.locator('#style-panel-modal');
  await expect(panel).toHaveClass(/show/);

  const sizeInput = panel.getByLabel('字号');
  await sizeInput.fill('60');
  await panel.locator('#style-panel-ok').click();
  await expect(panel).not.toHaveClass(/show/);

  const segs = await readSegments(page);
  expect(segs[0].overrides?.font_size).toBe(60);
  expect(segs[0].start).toBe(EXPECTED_SEGMENTS[0].start);
});

test('style manager opens from the toolbar button', async ({ page }) => {
  await page.goto(server.url);
  await page.locator('#style-manager-btn').click();
  const manager = page.locator('#style-manager-modal');
  await expect(manager).toHaveClass(/show/);
  // 工程样式至少含 Default
  await expect(manager.locator('#style-manager-project-list li.default')).toHaveText(/Default/);
  await manager.locator('#style-manager-close').click();
  await expect(manager).not.toHaveClass(/show/);
});

test('pos persists through a server save and reload, segments untouched', async ({ page }) => {
  await page.goto(server.url);
  const overlay = await revealOverlay(page);
  const box = await overlay.boundingBox();
  await page.mouse.move(box.x + box.width * 0.5, box.y + box.height * 0.5);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width * 0.5 + 55, box.y + box.height * 0.5 - 90, { steps: 8 });
  await page.mouse.up();
  const saved = (await readSegments(page))[0].overrides?.pos;
  expect(saved).toBeTruthy();

  await page.getByRole('button', { name: '保存工程', exact: true }).click();
  await expect.poll(() => page.evaluate(() => !hasUnsavedProjectChanges())).toBe(true);

  // 磁盘工程携带段级 overrides.pos，时间轴不受影响
  const onDisk = JSON.parse(readFileSync(projectPath, 'utf-8'));
  expect(onDisk.segments[0].overrides.pos).toEqual(saved);
  expect(onDiskSegmentTiming(onDisk.segments)).toEqual(EXPECTED_SEGMENTS);
  // 决策 43：旧全局预览几何不再写出
  expect(onDisk.preview?.subtitle).toBeUndefined();

  await page.reload();
  await revealOverlay(page);
  const reloaded = (await readSegments(page))[0].overrides?.pos;
  expect(reloaded).toEqual(saved);
  expect(onDiskSegmentTiming(await page.evaluate(() => DATA.segments))).toEqual(EXPECTED_SEGMENTS);
});

// ===========================================================================
// Portable blank-editor.html — import project+media, drag through the real
// user surface, export project JSON, reimport it, observe same per-segment
// pos with unchanged segment timing. No server; download captured via events.
// ===========================================================================
test.describe('portable HTML', () => {
  let portableDir;
  let portableStaticServer;
  let blankHtmlPath;
  let portableProjectPath;
  let portableWavPath;

  test.beforeAll(async () => {
    portableDir = makeTempDir('preview-geometry-portable');
    portableWavPath = join(portableDir, 'synthetic.wav');
    portableProjectPath = join(portableDir, 'project.json');
    blankHtmlPath = join(portableDir, 'blank-editor.html');
    generateWav(portableWavPath, DURATION_MS / 1000);
    generateProjectJson(portableProjectPath);
    generateBlankEditor(blankHtmlPath);
    portableStaticServer = await startStaticServer(blankHtmlPath, await findFreePort());
  });

  test.afterAll(async () => {
    await portableStaticServer?.stop();
    cleanupTempDir(portableDir);
  });

  async function loadProjectAndMedia(page, projectPath) {
    await page.locator('#open-project-file').setInputFiles(projectPath);
    const mediaModal = page.locator('#project-media-modal');
    await mediaModal.waitFor({ state: 'visible', timeout: 5000 });
    await page.locator('#project-media-select').click();
    await page.locator('#load-media-file').setInputFiles(portableWavPath);
    await page.waitForFunction(() => {
      const p = document.getElementById('player');
      const src = p?.currentSrc || p?.querySelector('source')?.getAttribute('src');
      return Boolean(src && src.trim());
    }, { timeout: 10_000 });
    await mediaModal.waitFor({ state: 'hidden', timeout: 5000 });
  }

  test('pos survives export + reimport through the real UI, segments untouched', async ({ page }) => {
    await page.addInitScript(() => { delete window.showSaveFilePicker; });
    await page.goto(portableStaticServer.url);
    await loadProjectAndMedia(page, portableProjectPath);

    const overlay = await revealOverlay(page);
    expect(await readSegments(page)).toEqual(
      EXPECTED_SEGMENTS.map((s) => ({ ...s, overrides: null, style_ref: null })));

    // --- Drag the overlay body up through the user surface ---
    const box = await overlay.boundingBox();
    expect(box).not.toBeNull();
    await page.mouse.move(box.x + box.width * 0.5, box.y + box.height * 0.5);
    await page.mouse.down();
    await page.mouse.move(box.x + box.width * 0.5 - 40, box.y + box.height * 0.5 - 110, { steps: 8 });
    await page.mouse.up();
    const afterDrag = (await readSegments(page))[0].overrides?.pos;
    expect(afterDrag).toBeTruthy();

    // --- Export project JSON via the real "导出工程" (#download-json) button ---
    const downloadPromise = page.waitForEvent('download');
    await page.locator('#download-json').click();
    const download = await downloadPromise;
    const exportedPath = join(portableDir, 'exported.json');
    await download.saveAs(exportedPath);

    const exported = JSON.parse(readFileSync(exportedPath, 'utf-8'));
    expect(exported.segments[0].overrides.pos).toEqual(afterDrag);
    expect(exported.preview?.subtitle).toBeUndefined();
    expect(onDiskSegmentTiming(exported.segments)).toEqual(EXPECTED_SEGMENTS);

    // --- Reimport the downloaded JSON through #open-project-file ---
    await page.reload();
    await page.locator('#open-project-file').setInputFiles(exportedPath);
    const mediaModal = page.locator('#project-media-modal');
    if (await mediaModal.isVisible().catch(() => false)) {
      await page.locator('#project-media-select').click();
      await page.locator('#load-media-file').setInputFiles(portableWavPath);
      await mediaModal.waitFor({ state: 'hidden', timeout: 5000 });
    }
    const reimported = (await readSegments(page))[0].overrides?.pos;
    expect(reimported).toEqual(afterDrag);
    expect(await readSegments(page)).not.toBeNull();
  });
});
