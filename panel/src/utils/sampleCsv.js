// Downloadable sample files for the four CSV imports (Retailers, Distributors,
// Products, Gifts).
//
// Imports match headers rather than dictating them, so these are only a
// starting point — a manufacturer's own export imports as-is. The headers below
// are picked from the server's alias tuples in app/main.py (_CSV_*_HEADERS) so
// a downloaded sample, filled in and re-uploaded, always resolves cleanly.
import { downloadCSV } from './csv.js'

const SAMPLES = {
  retailers: {
    filename: 'sample-retailers.csv',
    columns: [
      'Shop Name', 'Name', 'Mobile', 'City', 'Address', 'Distributor',
      'Point Balance', 'external_id',
    ],
    rows: [
      ['Kumar Sarees', 'Ramesh Kumar', '9812345670', 'Surat',
       '12 Ring Road, Katargam', 'Vansh Distributors', '1500', ''],
      ['Heritage Textiles', 'Anita Shah', '9898765432', 'Ahmedabad',
       'Shop 4, CG Road', 'Vansh Distributors', '0', ''],
      ['', 'Suresh Patel', '9765432100', 'Rajkot', '', '', '', ''],
    ],
  },
  distributors: {
    filename: 'sample-distributors.csv',
    columns: ['Name', 'Mobile', 'City'],
    rows: [
      ['Vansh Distributors', '9811112222', 'Surat'],
      ['Shreeji Agency', '', 'Ahmedabad'],
    ],
  },
  products: {
    filename: 'sample-products.csv',
    columns: ['Product Name', 'Product Code', 'Points', 'Category', 'Fabric'],
    rows: [
      ['Banarasi Silk Saree', 'BSS-1001', '50', 'Saree', 'Silk'],
      ['Cotton Kurti', 'CK-2044', '20', 'Kurti', 'Cotton'],
    ],
  },
  // Codes printed by a previous loyalty platform. Headers mirror that
  // platform's own export, which is what a manufacturer actually has to hand;
  // the server matches them via _CSV_QR_*_HEADERS.
  legacyQr: {
    filename: 'sample-existing-qr.csv',
    columns: [
      'ID', 'batch running code', 'unit code', 'scratch code', 'product code',
      'MRP', 'created at', 'QR status',
    ],
    rows: [
      ['100001', 'BR-1', 'http://gverify.me/?Your Brand-Qi7YhrsToYI0Dbew',
       '123451', 'BSS-1001', '0.00', '2025-04-11 10:22:31', '1'],
      ['100002', 'BR-1', 'http://gverify.me/?Your Brand-Zk2MnpQrStUv3Wxy',
       '123452', 'CK-2044', '0.00', '2025-04-11 10:22:31', '1'],
    ],
  },
  gifts: {
    filename: 'sample-rewards.csv',
    columns: ['Name', 'Points', 'Description', 'Image'],
    rows: [
      ['LED TV (32 Inch)', '25000', 'Samsung / Mi / Similar Brand', ''],
      ['Mixer Grinder', '6000', '3 jar, 750W', ''],
    ],
  },
}

// The two blank cells in the retailer sample are intentional: a row with no
// shop falls back to the person's name, and phone/balance stay optional.
export function downloadSample(kind) {
  const s = SAMPLES[kind]
  if (!s) return
  downloadCSV(
    s.filename,
    s.columns.map((label, i) => ({ label, key: String(i) })),
    s.rows.map((r) => Object.fromEntries(r.map((v, i) => [String(i), v]))),
  )
}
