// Load a skin pack: skin.json manifest + sprite sheet image.
// Skin layout: ui/skins/<name>/skin.json + sheet.png (frames left-to-right).
export async function loadSkin(base = "./skins/default") {
  const manifest = await fetch(`${base}/skin.json`).then((r) => {
    if (!r.ok) throw new Error(`skin manifest missing: ${base}/skin.json`);
    return r.json();
  });
  const image = await new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => resolve(img);
    img.onerror = () => reject(new Error(`sprite sheet missing: ${base}/sheet.png`));
    img.src = `${base}/sheet.png`;
  });
  return { manifest, image };
}
