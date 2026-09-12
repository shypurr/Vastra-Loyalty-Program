import { useCallback, useEffect, useRef, useState } from 'react'
import { del, get, post, put } from '../api.js'
import GenerateQrModal from '../components/GenerateQrModal.jsx'
import ImportResult, { ColumnMap } from '../components/ImportResult.jsx'
import EmptyState from '../components/EmptyState.jsx'
import SearchBox from '../components/SearchBox.jsx'
import { useConfirm } from '../confirm.jsx'
import { downloadSample } from '../utils/sampleCsv.js'
import { downloadCSV, today, chunkCsv } from '../utils/csv.js'
import { ToolbarButton, OverflowMenu } from '../components/Toolbar.jsx'
import {
  IconImport, IconExport, IconSample, IconTrash, IconQr,
} from '../components/icons.jsx'

// The catalog is the manufacturer's own CSV import. Columns are whatever their
// file contained: the API returns `columns` (the free-form headers, in CSV
// order) alongside the products, and the table renders name/code, then those,
// then Points and Actions.
const CSV_HELP =
  'CSV needs a product name column and a product code column (any spelling, ' +
  'any case). A points column is optional. Every other column is kept and ' +
  'shown here.'

const PAGE_SIZES = [10, 25, 50, 100, 500]

export default function Products() {
  const [catalog, setCatalog] = useState(null) // {products, columns, source}
  const [editing, setEditing] = useState(null) // {external_id, points}
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)
  const [showGenerate, setShowGenerate] = useState(false)
  const [importResult, setImportResult] = useState(null)
  const [pendingCsv, setPendingCsv] = useState(null) // awaiting update/replace
  const [pageSize, setPageSize] = useState(PAGE_SIZES[0])
  const [page, setPage] = useState(0)
  const [query, setQuery] = useState('')
  const fileRef = useRef(null)
  // Second file input: existing QR codes printed by a previous platform. Kept
  // separate from the catalog import so neither can be fed the other's file.
  const legacyRef = useRef(null)
  const [legacy, setLegacy] = useState(null)     // aggregated import result
  const [legacyPct, setLegacyPct] = useState(null) // null = not running
  const confirm = useConfirm()

  const products = catalog?.products ?? []
  const attrCols = catalog?.columns ?? []
  const imported = catalog?.source === 'import'

  // Name or product code only — the CSV's own columns are deliberately not
  // searched, so a query can't match something the manufacturer can't see.
  const q = query.trim().toLowerCase()
  const filtered = q
    ? products.filter((p) =>
        `${p.name ?? ''} ${p.sku ?? ''}`.toLowerCase().includes(q),
      )
    : products

  const pages = Math.max(1, Math.ceil(filtered.length / pageSize))
  const from = page * pageSize
  const visible = filtered.slice(from, from + pageSize)

  // Deleting or re-importing can shrink the list out from under the current
  // page; land on the last real one rather than an empty table.
  useEffect(() => {
    if (page >= pages) setPage(pages - 1)
  }, [page, pages])

  const load = useCallback(() => {
    setError(null)
    get('/catalog/products').then(setCatalog).catch((e) => setError(e.message))
  }, [])

  useEffect(load, [load])

  const runImport = async (csv, mode) => {
    setBusy(true)
    setError(null)
    setImportResult(null)
    setPendingCsv(null)
    try {
      setImportResult(await post('/catalog/products/import', { csv, mode }))
      load()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  const onImportFile = async (e) => {
    const file = e.target.files?.[0]
    if (file) e.target.value = '' // allow re-importing the same filename
    if (!file) return
    setError(null)
    let csv
    try {
      csv = await file.text()
    } catch (err) {
      setError(`Could not read ${file.name}: ${err.message}`)
      return
    }
    // The first import just runs. Once a list exists, the manufacturer decides
    // whether the file updates it or replaces it wholesale.
    if (imported) setPendingCsv({ csv, name: file.name })
    else runImport(csv, 'upsert')
  }

  // Chunked on purpose: a legacy export can run to six figures of codes, which
  // is far past what one request should carry. Each chunk is independently
  // idempotent server-side, so a failure part-way can simply be re-run.
  const onImportLegacyFile = async (e) => {
    const file = e.target.files?.[0]
    if (file) e.target.value = ''
    if (!file) return
    setError(null)
    setLegacy(null)
    let text
    try {
      text = await file.text()
    } catch (err) {
      setError(`Could not read ${file.name}: ${err.message}`)
      return
    }
    const chunks = chunkCsv(text, 500)
    if (!chunks.length) {
      setError(`${file.name} has no rows.`)
      return
    }
    const total = { created: 0, skipped: 0, redeemed: 0, errors: [],
                    columns: null, brand: '' }
    setLegacyPct(0)
    try {
      for (let i = 0; i < chunks.length; i++) {
        const res = await post('/qr/codes/import', { csv: chunks[i] })
        total.created += res.created ?? 0
        total.skipped += res.skipped ?? 0
        total.redeemed += res.redeemed ?? 0
        total.errors.push(...(res.errors ?? []))
        total.columns = total.columns ?? res.columns
        total.brand = total.brand || res.brand
        setLegacyPct(Math.round(((i + 1) / chunks.length) * 100))
      }
      setLegacy(total)
    } catch (err) {
      // Report what did land: the codes already imported are committed, and
      // re-running the same file skips them rather than duplicating.
      setError(
        `${err.message} — ${total.created} code(s) imported before the ` +
        'failure. Re-running the same file is safe: it will skip those.',
      )
      if (total.created || total.skipped) setLegacy(total)
    } finally {
      setLegacyPct(null)
    }
  }

  const confirmReplace = async () => {
    const ok = await confirm({
      title: 'Replace the entire product list?',
      message:
        `All ${products.length} product(s) currently listed are removed and ` +
        'replaced by the file. Points you set here are lost. Already-printed ' +
        'QR codes keep working — they carry their own product details.',
      confirmLabel: 'Replace all',
      danger: true,
    })
    if (ok) runImport(pendingCsv.csv, 'replace')
  }

  const remove = async (p) => {
    const ok = await confirm({
      title: `Delete ${p.name}?`,
      message:
        'It is removed from your product list. Already-printed QR codes for ' +
        'it keep working — they carry their own product details.',
      confirmLabel: 'Delete',
      danger: true,
    })
    if (!ok) return
    setBusy(true)
    setError(null)
    try {
      await del(`/catalog/products/${encodeURIComponent(p.external_id)}`)
      load()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  const removeAll = async () => {
    const ok = await confirm({
      title: `Delete all ${products.length} products?`,
      confirmLabel: `Delete all ${products.length}`,
      danger: true,
    })
    if (!ok) return
    setBusy(true)
    setError(null)
    try {
      await del('/catalog/products')
      setImportResult(null)
      load()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  const savePoints = async () => {
    setBusy(true)
    setError(null)
    try {
      await put(
        `/catalog/products/${encodeURIComponent(editing.external_id)}/points`,
        { points: Number(editing.points) },
      )
      setEditing(null)
      load()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  const exportCsv = () => {
    downloadCSV(
      `products-${today()}.csv`,
      [
        { label: 'Product name', key: 'name' },
        { label: 'Product code', key: 'sku' },
        ...attrCols.map((c) => ({ label: c, key: c })),
        { label: 'Points', key: 'points' },
      ],
      // Exports what the search is currently showing, not just the page.
      // Fixed fields last so they win if a CSV column shared their key.
      filtered.map((p) => ({
        ...p.attrs,
        name: p.name,
        sku: p.sku,
        points: p.points,
      })),
    )
  }

  if (!catalog && !error) return <p className="loading">Loading…</p>

  return (
    <div className="products">
      <div className="schemes-head">
        <h2 className="page-title">
          Products{' '}
          {/* Reflects the search, so the number always matches the rows. */}
          <span className="count">
            {q ? `${filtered.length} / ${products.length}` : products.length}
          </span>
        </h2>
        <div className="btn-row">
          <input
            ref={fileRef}
            type="file"
            accept=".csv,text/csv"
            style={{ display: 'none' }}
            onChange={onImportFile}
          />
          <input
            ref={legacyRef}
            type="file"
            accept=".csv,text/csv"
            style={{ display: 'none' }}
            onChange={onImportLegacyFile}
          />
          {products.length > 0 && (
            <ToolbarButton icon={<IconQr />} onClick={() => setShowGenerate(true)}>
              Generate QR
            </ToolbarButton>
          )}
          <ToolbarButton
            icon={<IconImport />}
            disabled={busy}
            onClick={() => fileRef.current?.click()}
            title={CSV_HELP}
          >
            Import CSV
          </ToolbarButton>
          <OverflowMenu
            items={[
              {
                label: 'Export CSV',
                icon: <IconExport />,
                show: products.length > 0,
                onClick: exportCsv,
              },
              {
                label: 'Sample CSV',
                icon: <IconSample />,
                title: 'Download a filled-in example file with the expected columns',
                onClick: () => downloadSample('products'),
              },
              {
                label:
                  legacyPct === null
                    ? 'Import existing QR'
                    : `Importing… ${legacyPct}%`,
                icon: <IconQr />,
                title:
                  'Load QR codes already printed by your previous platform, ' +
                  'so stickers already in the market keep working',
                // Needs a real imported catalog: each code takes its points
                // from its product, and sample products are not rows.
                show: imported && products.length > 0,
                disabled: busy || legacyPct !== null,
                onClick: () => legacyRef.current?.click(),
              },
              {
                label: 'Sample existing-QR CSV',
                icon: <IconSample />,
                title: 'Example of the code list exported by a previous platform',
                show: imported && products.length > 0,
                onClick: () => downloadSample('legacyQr'),
              },
              {
                label: 'Delete all',
                icon: <IconTrash />,
                danger: true,
                // Only an imported catalog can be cleared; samples aren't rows.
                show: imported && products.length > 0,
                disabled: busy,
                onClick: removeAll,
              },
            ]}
          />
        </div>
      </div>

      <p className="hint">
        {CSV_HELP} Points per scan are set by you and affect{' '}
        <strong>future batches only</strong> — already printed batches keep
        their promised points.
      </p>

      {catalog?.source === 'sample' && (
        <p className="hint">
          <strong>Sample products.</strong> These are built-in examples for
          testing. Import your CSV and they are replaced by your own catalog.
        </p>
      )}

      {error && <p className="error">{error}</p>}
      {legacy && (
        <div className="panel-card" style={{ marginBottom: 14 }}>
          <div className="schemes-head" style={{ marginBottom: 6 }}>
            <strong>
              Existing QR codes — imported {legacy.created} · skipped{' '}
              {legacy.skipped}
              {legacy.errors?.length
                ? ` · ${legacy.errors.length} error(s)`
                : ''}
            </strong>
            <button className="btn-ghost small" onClick={() => setLegacy(null)}>
              Dismiss
            </button>
          </div>
          <p className="hint" style={{ marginTop: 0 }}>
            {legacy.brand ? (
              <>
                Brand <strong>{legacy.brand}</strong>.{' '}
              </>
            ) : null}
            {/* Skipped is the expected result of re-running a file, not a
                fault — say so, or it reads as data loss. */}
            Skipped codes were already imported; re-running the same file is
            safe.
            {legacy.redeemed
              ? ` ${legacy.redeemed} code(s) were already scanned on the old
                 platform and were imported as used.`
              : ''}
          </p>
          <ColumnMap columns={legacy.columns} />
          {legacy.errors?.length > 0 && (
            <ul className="hint" style={{ marginTop: 0 }}>
              {legacy.errors.slice(0, 8).map((er, i) => (
                <li key={i}>{er}</li>
              ))}
            </ul>
          )}
        </div>
      )}

      <ImportResult
        result={importResult}
        onDismiss={() => setImportResult(null)}
      />

      {catalog?.source === 'empty' && (
        <div className="panel-card">
          <EmptyState
            icon="📦"
            title="No products yet"
            message="Import your product list as a CSV. It only needs a product name and a product code — every other column you have is kept and shown here."
            action={
              <button
                className="btn-primary"
                disabled={busy}
                onClick={() => fileRef.current?.click()}
              >
                Import CSV
              </button>
            }
          />
        </div>
      )}

      {products.length > 0 && (
        <div className="panel-card table-card">
          <div className="table-toolbar">
            <SearchBox
              placeholder="Search product name or code…"
              value={query}
              onChange={(v) => {
                setQuery(v)
                setPage(0)
              }}
            />
            <span>
              {visible.length > 0 ? (
                <>
                  Showing <strong>{from + 1}</strong>–
                  <strong>{from + visible.length}</strong> of{' '}
                  <strong>{filtered.length}</strong>
                </>
              ) : (
                <>No matches</>
              )}
              {q && ` (filtered from ${products.length})`}
            </span>
            <label>
              Rows{' '}
              <select
                value={pageSize}
                onChange={(e) => {
                  setPageSize(Number(e.target.value))
                  setPage(0)
                }}
              >
                {PAGE_SIZES.map((n) => (
                  <option key={n} value={n}>
                    {n}
                  </option>
                ))}
              </select>
            </label>
          </div>

          <div className="catalog-scroll">
            <table className="data-table catalog-table">
              <thead>
                <tr>
                  <th className="sticky-col col-name">
                    <div className="cell">Product name</div>
                  </th>
                  <th className="sticky-col col-code">
                    <div className="cell">Product code</div>
                  </th>
                  {attrCols.map((c) => (
                    <th key={c}>{c}</th>
                  ))}
                  <th className="sticky-col col-points num">
                    <div className="cell">Points</div>
                  </th>
                  <th className="sticky-col col-actions">
                    <div className="cell">Actions</div>
                  </th>
                </tr>
              </thead>
              <tbody>
                {visible.map((p) => {
                  const isEditing = editing?.external_id === p.external_id
                  return (
                    <tr
                      key={p.external_id}
                      className={isEditing ? 'editing-row' : undefined}
                    >
                      <td className="sticky-col col-name">
                        <div className="cell" title={p.name}>
                          {p.name}
                        </div>
                      </td>
                      <td className="sticky-col col-code mono">
                        <div className="cell" title={p.sku}>
                          {p.sku}
                        </div>
                      </td>
                      {attrCols.map((c) => (
                        <td key={c}>{p.attrs?.[c] ?? ''}</td>
                      ))}
                      <td className="sticky-col col-points num">
                        <div className="cell">
                          {isEditing ? (
                            <input
                              className="inline-input num"
                              type="number"
                              min="0"
                              autoFocus
                              value={editing.points}
                              onChange={(e) =>
                                setEditing({
                                  ...editing,
                                  points: e.target.value,
                                })
                              }
                            />
                          ) : (
                            <strong>{p.points}</strong>
                          )}
                        </div>
                      </td>
                      <td className="sticky-col col-actions">
                        <div className="cell">
                          {isEditing ? (
                            <>
                              <button
                                className="btn-ghost small"
                                onClick={savePoints}
                                disabled={busy}
                              >
                                Save
                              </button>
                              <button
                                className="btn-ghost small"
                                onClick={() => setEditing(null)}
                              >
                                Cancel
                              </button>
                            </>
                          ) : (
                            <>
                              <button
                                className="btn-ghost small"
                                onClick={() =>
                                  setEditing({
                                    external_id: p.external_id,
                                    points: p.points,
                                  })
                                }
                              >
                                Edit points
                              </button>
                              {imported && (
                                <button
                                  className="btn-ghost small danger"
                                  disabled={busy}
                                  onClick={() => remove(p)}
                                >
                                  Delete
                                </button>
                              )}
                            </>
                          )}
                        </div>
                      </td>
                    </tr>
                  )
                })}
                {visible.length === 0 && (
                  <tr>
                    <td colSpan={attrCols.length + 4}>
                      <EmptyState
                        compact
                        icon="🔍"
                        title={`No products match “${query.trim()}”`}
                        message="Search covers the product name and code."
                        action={
                          <button
                            className="btn-ghost"
                            onClick={() => {
                              setQuery('')
                              setPage(0)
                            }}
                          >
                            Clear search
                          </button>
                        }
                      />
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>

          {pages > 1 && (
            <div className="pager">
              <button
                className="btn-ghost"
                disabled={page === 0}
                onClick={() => setPage((p) => p - 1)}
              >
                ← Prev
              </button>
              <span>
                Page {page + 1} of {pages}
              </span>
              <button
                className="btn-ghost"
                disabled={page + 1 >= pages}
                onClick={() => setPage((p) => p + 1)}
              >
                Next →
              </button>
            </div>
          )}
        </div>
      )}

      {pendingCsv && (
        <div className="modal-backdrop" onClick={() => setPendingCsv(null)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <h3>Import {pendingCsv.name}</h3>
            <p className="hint">
              You already have {products.length} product(s). Update the list
              with this file, or replace it entirely?
            </p>
            <div className="btn-row">
              <button
                className="btn-primary"
                disabled={busy}
                onClick={() => runImport(pendingCsv.csv, 'upsert')}
              >
                Update existing list
              </button>
              <button
                className="btn-secondary"
                disabled={busy}
                onClick={confirmReplace}
              >
                Replace entire list
              </button>
              <button
                className="btn-ghost"
                onClick={() => setPendingCsv(null)}
              >
                Cancel
              </button>
            </div>
            <p className="hint" style={{ marginBottom: 0 }}>
              <strong>Update</strong> refreshes products whose code matches,
              adds new ones, and deletes nothing — points you set here are kept
              unless the file has its own points column.
            </p>
          </div>
        </div>
      )}

      {showGenerate && (
        <GenerateQrModal
          products={products}
          onClose={() => setShowGenerate(false)}
        />
      )}
    </div>
  )
}
