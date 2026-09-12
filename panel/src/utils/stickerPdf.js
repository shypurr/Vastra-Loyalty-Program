// Client-side sticker sheet — a direct port of the server-side layout in
// app/pdf_service.py. Rendering moved into the browser because the PDF grows
// ~5.2 KB per code, so a 2,000-sticker batch is 10.3 MB and Lambda cannot
// return it (6 MB buffered / 20 MB streamed). The same batch as JSON is
// ~180 KB, and the PNG rendering no longer burns Lambda CPU.
//
// The geometry below is the same A4 grid, the same fonts and the same sizes as
// the Python version, so printed output is unchanged. One coordinate
// difference: reportlab measures y from the bottom of the page, jsPDF from the
// top, so every y here is expressed downward from the page top.
//
// The QR payload string is always taken from the API response verbatim and is
// never rebuilt from a token: QR_BASE_URL is server-side config, and a
// frontend copy that drifted would send every printed sticker to a dead host.
import { jsPDF } from 'jspdf'
import QRCode from 'qrcode'

const PAGE_W = 210 // A4, mm
const PAGE_H = 297
const MARGIN = 12

// item sticker grid
const COLS = 4
const ROWS = 6
const CELL_W = (PAGE_W - 2 * MARGIN) / COLS
const CELL_H = (PAGE_H - 2 * MARGIN) / ROWS
const QR_SIZE = Math.min(CELL_W, CELL_H) - 14

// box (parent) sticker grid — fewer, larger. The QR is kept smaller than the
// cell so the title sits above it and the product name + code fit below it,
// all inside the border.
const BCOLS = 2
const BROWS = 3
const BCELL_W = (PAGE_W - 2 * MARGIN) / BCOLS
const BCELL_H = (PAGE_H - 2 * MARGIN) / BROWS
const BQR_SIZE = Math.min(BCELL_W, BCELL_H) - 38

const fmt = (code) => `${code.slice(0, 3)}-${code.slice(3)}`

// Black/white only, never tinted — scanner reliability depends on hard module
// edges (same rule as app/qr_service.py). errorCorrectionLevel/margin match
// ERROR_CORRECT_M and border=4 server-side so the module grid is identical.
const qrPng = (payload, scale) =>
  QRCode.toDataURL(payload, {
    errorCorrectionLevel: 'M',
    margin: 4,
    scale,
    color: { dark: '#000000ff', light: '#ffffffff' },
  })

/** Flatten either API shape into one list of {payload, manual_code, token,
 *  items} — items 0 = item sticker, items > 0 = box sticker covering that
 *  many items. POST /qr/generate splits children and boxes into two arrays;
 *  GET /qr/batches/{id} returns one array carrying is_parent. */
export function stickerCodes(src) {
  if (src.boxes_codes) {
    return [
      ...src.codes.map((c) => ({ ...c, items: 0 })),
      ...src.boxes_codes,
    ]
  }
  return src.codes.map((c) => ({ ...c, items: c.is_parent ? c.items : 0 }))
}

// Why a scope exists: a 1,000-item batch prints every child sticker plus the
// box stickers, and the resulting PDF is unusable (and sometimes fails to
// build at all) when all the manufacturer wanted was the box codes to put on
// cartons. Filtering happens at print time, not generation time, so child
// codes still exist, a box scan still credits all of them, and a saved batch
// can be reprinted at a different scope months later.
const EMPTY_FOR_SCOPE = {
  all: 'This batch has no codes to print.',
  boxes:
    'This batch has no box codes — it was generated without an items-per-box ' +
    'value, so there is nothing to print at this scope.',
  items: 'This batch has no item codes to print.',
}

/** Build the sheet and hand the browser the finished file. `scope` is one of
 *  'all' | 'boxes' | 'items'. onProgress gets a 0-100 percentage — a 2,000-code
 *  run takes ~10-20s, so the caller needs to show something. */
export async function buildStickerPdf({
  productName,
  sku,
  codes,
  filename,
  scope = 'all',
  onProgress,
}) {
  // Applied at this split rather than in the caller, so `total` — and with it
  // the progress percentage — stays correct for every scope for free.
  const children = scope === 'boxes' ? [] : codes.filter((c) => !c.items)
  const parents = scope === 'items' ? [] : codes.filter((c) => c.items > 0)
  const total = children.length + parents.length
  if (!total) throw new Error(EMPTY_FOR_SCOPE[scope] ?? EMPTY_FOR_SCOPE.all)

  const doc = new jsPDF({ unit: 'mm', format: 'a4' })
  doc.setProperties({ title: `Loyalty QR - ${productName}` })

  let done = 0
  const tick = async () => {
    done += 1
    if (onProgress) onProgress(Math.round((done / total) * 100))
    // Yield to the event loop so the tab paints progress instead of freezing.
    if (done % 25 === 0) await new Promise((r) => setTimeout(r, 0))
  }

  const perPage = COLS * ROWS
  for (let i = 0; i < children.length; i++) {
    const c = children[i]
    const slot = i % perPage
    if (i > 0 && slot === 0) doc.addPage()
    const cellX = MARGIN + (slot % COLS) * CELL_W
    const cellTop = MARGIN + Math.floor(slot / COLS) * CELL_H

    // Centred in the cell, then nudged up 4mm to leave room for the two
    // label lines below it.
    const qrX = cellX + (CELL_W - QR_SIZE) / 2
    const qrTop = cellTop + (CELL_H - QR_SIZE) / 2 - 4
    doc.addImage(await qrPng(c.payload, 10), 'PNG',
                 qrX, qrTop, QR_SIZE, QR_SIZE, c.token, 'FAST')

    doc.setFont('helvetica', 'bold')
    doc.setFontSize(7)
    doc.text(`${productName} (${sku})`, cellX + CELL_W / 2,
             qrTop + QR_SIZE + 3, { align: 'center' })
    doc.setFontSize(10)
    doc.text(fmt(c.manual_code), cellX + CELL_W / 2,
             qrTop + QR_SIZE + 8, { align: 'center' })
    await tick()
  }

  if (parents.length) {
    if (children.length) doc.addPage()
    const bPerPage = BCOLS * BROWS
    for (let i = 0; i < parents.length; i++) {
      const c = parents[i]
      const slot = i % bPerPage
      if (i > 0 && slot === 0) doc.addPage()
      const cellX = MARGIN + (slot % BCOLS) * BCELL_W
      const cellTop = MARGIN + Math.floor(slot / BCOLS) * BCELL_H
      const cx = cellX + BCELL_W / 2

      // border so a box sticker is unmistakable on the carton
      doc.setLineWidth(0.42) // 1.2pt, expressed in mm
      doc.rect(cellX + 5, cellTop + 5, BCELL_W - 10, BCELL_H - 10)

      // Title sits just below the top border.
      doc.setFont('helvetica', 'bold')
      doc.setFontSize(13)
      doc.text(`BOX — ${c.items} items`, cx, cellTop + 14, { align: 'center' })

      // QR shifted up so both labels fit below it, above the bottom line.
      const qrTop = cellTop + BCELL_H - 22 - BQR_SIZE
      doc.addImage(await qrPng(c.payload, 14), 'PNG',
                   cx - BQR_SIZE / 2, qrTop, BQR_SIZE, BQR_SIZE, c.token,
                   'FAST')

      // Product name then manual code, both inside the border.
      doc.setFontSize(8)
      doc.text(`${productName} (${sku})`, cx, cellTop + BCELL_H - 14,
               { align: 'center' })
      doc.setFontSize(12)
      doc.text(fmt(c.manual_code), cx, cellTop + BCELL_H - 8,
               { align: 'center' })
      await tick()
    }
  }

  doc.save(filename)
}
