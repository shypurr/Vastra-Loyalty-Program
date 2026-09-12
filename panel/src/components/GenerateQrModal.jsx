import { useEffect, useState } from 'react'
import { get, post } from '../api.js'
import { buildStickerPdf, stickerCodes } from '../utils/stickerPdf.js'

// In-panel QR generation — replaces the old standalone /web/generate webview so
// the manufacturer never leaves the Products page. Sends the primary
// /qr/generate contract (product_external_id + snapshot + points_per_code):
// the catalog is the manufacturer's CSV import, so there's no products-table
// lookup. `products` comes from GET /catalog/products.

// Print scope. "Box codes only" exists because a 1,000-item batch otherwise
// prints every child sticker alongside the box stickers, producing a PDF that
// is slow to build and unwieldy to handle when only the carton codes are
// wanted. Filtering is applied at print time, so child codes still exist, a box
// scan still credits all of them, and a batch can be reprinted at a different
// scope later.
const PRINT_SCOPES = [
  { id: 'all', label: 'All codes', unit: 'stickers',
    count: (b) => (b.quantity ?? 0) + (b.boxes ?? 0) },
  { id: 'boxes', label: 'Box codes only', unit: 'boxes',
    count: (b) => b.boxes ?? 0 },
  { id: 'items', label: 'Item codes only', unit: 'items',
    count: (b) => b.quantity ?? 0 },
]

export default function GenerateQrModal({ products, onClose }) {
  const [productExternalId, setProductExternalId] = useState(
    products[0]?.external_id ?? '',
  )
  const [pointsPerCode, setPointsPerCode] = useState(products[0]?.points ?? 0)
  const [quantity, setQuantity] = useState(10)
  const [itemsPerBox, setItemsPerBox] = useState('')
  const [batch, setBatch] = useState(null) // generation result
  const [saved, setSaved] = useState(false)
  const [showSaved, setShowSaved] = useState(false)
  const [savedBatches, setSavedBatches] = useState(null)
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)
  // Rendering runs in this tab now, so it needs a visible progress state —
  // 2,000 codes takes ~10-20s where the server PDF was a single tab-open.
  const [printing, setPrinting] = useState(null) // batch id being rendered
  const [progress, setProgress] = useState(0)
  // Two scopes, because the two print surfaces are independent: the radio
  // group belongs to the batch just generated, the select to the saved list.
  const [scope, setScope] = useState('all')
  const [savedScope, setSavedScope] = useState('all')

  // Close on Escape, matching the other panel modals.
  useEffect(() => {
    const onEsc = (e) => e.key === 'Escape' && onClose()
    document.addEventListener('keydown', onEsc)
    return () => document.removeEventListener('keydown', onEsc)
  }, [onClose])

  const selectProduct = (externalId) => {
    setProductExternalId(externalId)
    const product = products.find((p) => p.external_id === externalId)
    setPointsPerCode(product?.points ?? 0)
  }

  const generate = async () => {
    setBusy(true)
    setError(null)
    try {
      const product = products.find((p) => p.external_id === productExternalId)
      const ipb = Number(itemsPerBox)
      const payload = {
        product_external_id: productExternalId,
        product_name: product.name,
        product_sku: product.sku,
        points_per_code: Number(pointsPerCode),
        quantity: Number(quantity),
      }
      if (ipb >= 2) payload.items_per_box = ipb
      const res = await post('/qr/generate', payload)
      setBatch(res)
      setSaved(false)
      setScope('all') // the new batch may have no boxes at all
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  const saveBatch = async () => {
    setError(null)
    try {
      await post(batch.actions.save)
      setSaved(true)
      if (showSaved) loadSaved()
    } catch (err) {
      setError(err.message)
    }
  }

  // The sheet is built in the browser, not fetched from /qr/batches/{id}/print
  // — a 2,000-code PDF is 10.3 MB and Lambda cannot return it. `data` is the
  // freshly generated batch, which already carries its codes; a saved batch is
  // fetched first. Either way the code list (including each code's server-built
  // payload URL) comes from the API, and buildStickerPdf just lays it out.
  const printBatch = async (batchId, data, printScope = 'all') => {
    setError(null)
    setPrinting(batchId)
    setProgress(0)
    try {
      const src = data ?? (await get(`/qr/batches/${batchId}`))
      await buildStickerPdf({
        productName: src.product_name,
        sku: src.product_sku,
        codes: stickerCodes(src),
        // Scope in the name so printing boxes and items from the same batch
        // gives two distinct files instead of "…(1).pdf".
        filename: `loyalty-qr-batch-${batchId}${
          printScope === 'all' ? '' : `-${printScope}`
        }.pdf`,
        scope: printScope,
        onProgress: setProgress,
      })
    } catch (err) {
      setError(err.message)
    } finally {
      setPrinting(null)
    }
  }

  const newBatch = () => {
    setBatch(null)
    setSaved(false)
    setError(null)
    setScope('all')
  }

  const loadSaved = () => {
    get('/qr/batches?status=saved').then(setSavedBatches).catch(() => {})
  }

  const toggleSaved = () => {
    const next = !showSaved
    setShowSaved(next)
    if (next && savedBatches === null) loadSaved()
  }

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <h3>Generate loyalty QR</h3>

        {!batch ? (
          <>
            <p className="hint" style={{ margin: 0 }}>
              Points are frozen at generation — already-printed batches keep
              their value even if the product points change later.
            </p>
            <label>
              Product
              <select
                value={productExternalId}
                onChange={(e) => selectProduct(e.target.value)}
              >
                {products.map((p) => (
                  <option key={p.external_id} value={p.external_id}>
                    {p.name} — {p.points} pts
                  </option>
                ))}
              </select>
            </label>
            <label>
              Points per scan
              <input
                type="number"
                min="0"
                value={pointsPerCode}
                onChange={(e) => setPointsPerCode(e.target.value)}
              />
            </label>
            <label>
              Quantity
              <input
                type="number"
                min="1"
                max="10000"
                value={quantity}
                onChange={(e) => setQuantity(e.target.value)}
              />
            </label>
            <label>
              Items per box{' '}
              <span className="hint">(optional — makes box QR codes)</span>
              <input
                type="number"
                min="2"
                max="1000"
                value={itemsPerBox}
                onChange={(e) => setItemsPerBox(e.target.value)}
                placeholder="leave blank for none"
              />
            </label>
            {error && <p className="error">{error}</p>}
            <div className="btn-row">
              <button
                className="btn-primary"
                disabled={busy || !productExternalId}
                onClick={generate}
              >
                {busy ? 'Generating…' : 'Generate QR codes'}
              </button>
              <button className="btn-ghost" onClick={toggleSaved}>
                {showSaved ? 'Hide saved batches' : 'Saved batches'}
              </button>
              <button className="btn-ghost" onClick={onClose}>
                Close
              </button>
            </div>
          </>
        ) : (
          <>
            <p style={{ margin: '4px 0' }}>
              <strong>{batch.quantity}</strong> codes for{' '}
              <strong>{batch.product_name}</strong>
            </p>
            <p style={{ margin: '4px 0' }}>
              {batch.points_per_code} points per scan
            </p>
            {batch.boxes ? (
              <p style={{ margin: '4px 0', color: 'var(--leaf)' }}>
                + {batch.boxes} box QR codes ({batch.items_per_box} items each)
              </p>
            ) : null}
            <p className="mono sub" style={{ margin: '4px 0' }}>
              batch #{batch.batch_id}
            </p>
            <div className="scope-group">
              <span className="scope-title">Print</span>
              {PRINT_SCOPES.map((s) => {
                const n = s.count(batch)
                return (
                  <label
                    key={s.id}
                    className={`scope-opt${n ? '' : ' disabled'}`}
                  >
                    <input
                      type="radio"
                      name="print-scope"
                      value={s.id}
                      checked={scope === s.id}
                      disabled={!n}
                      onChange={() => setScope(s.id)}
                    />
                    <span>{s.label}</span>
                    <span className="sub">
                      · {n} {s.unit}
                    </span>
                  </label>
                )
              })}
            </div>
            {error && <p className="error">{error}</p>}
            <div className="btn-row">
              <button
                className="btn-primary"
                disabled={printing !== null}
                onClick={() => printBatch(batch.batch_id, batch, scope)}
              >
                {printing === batch.batch_id
                  ? `Building PDF… ${progress}%`
                  : 'Print stickers (PDF)'}
              </button>
              <button
                className="btn-secondary"
                disabled={saved}
                onClick={saveBatch}
              >
                {saved ? 'Saved ✓' : 'Save for later'}
              </button>
              <button className="btn-ghost" onClick={newBatch}>
                New batch
              </button>
            </div>
          </>
        )}

        {showSaved && (
          <div className="panel-card" style={{ marginTop: 14 }}>
            <div className="saved-head">
              <strong style={{ fontFamily: "'Helvetica Neue', Helvetica, Arial, sans-serif" }}>
                Saved batches
              </strong>
              <select
                className="scope-select"
                value={savedScope}
                onChange={(e) => setSavedScope(e.target.value)}
                aria-label="Print scope for saved batches"
              >
                {PRINT_SCOPES.map((s) => (
                  <option key={s.id} value={s.id}>
                    {s.label}
                  </option>
                ))}
              </select>
            </div>
            {savedBatches === null ? (
              <p className="hint" style={{ margin: '8px 0 0' }}>
                Loading…
              </p>
            ) : savedBatches.length === 0 ? (
              <p className="hint" style={{ margin: '8px 0 0' }}>
                No saved batches yet.
              </p>
            ) : (
              <table className="data-table" style={{ marginTop: 8 }}>
                <tbody>
                  {savedBatches.map((b) => (
                    <tr key={b.id}>
                      <td>
                        {b.product_name}
                        <span className="sub">
                          {' '}
                          · {b.quantity} codes
                          {b.boxes ? ` · ${b.boxes} boxes` : ''} ·{' '}
                          {b.points_per_code} pts ·{' '}
                          {b.created_at?.slice(0, 10)} · #{b.id}
                        </span>
                      </td>
                      <td className="actions-col">
                        {savedScope === 'boxes' && !b.boxes ? (
                          // Nothing to print at this scope — say so rather than
                          // offering a button that can only fail.
                          <span className="sub">no boxes</span>
                        ) : (
                          <button
                            className="btn-ghost small"
                            disabled={printing !== null}
                            onClick={() => printBatch(b.id, null, savedScope)}
                          >
                            {printing === b.id ? `${progress}%` : 'Print'}
                          </button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        )}
      </div>
    </div>
  )
}
