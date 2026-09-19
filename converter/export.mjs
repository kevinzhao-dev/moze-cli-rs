// Input MUST be a disposable private copy: Realm may upgrade its file format.
import Realm from 'realm';
import fs from 'node:fs';
process.umask(0o077);
let realm;
try {
  realm = new Realm({path: process.argv[2]});
  const schemas = new Map(realm.schema.map(s => [s.name, s]));
  function value(v, property = {}) {
    if (v == null) return null;
    if (v instanceof Date) return {$date: v.toISOString()};
    if (v instanceof ArrayBuffer || ArrayBuffer.isView(v)) return {$binary: Buffer.from(v instanceof ArrayBuffer ? v : v.buffer, v.byteOffset ?? 0, v.byteLength).toString('base64')};
    if (v instanceof Realm.Object) {
      const s = v.objectSchema();
      if (!s.primaryKey) throw new Error('Nonempty model without primary key is unsupported');
      return {$ref: s.name, id: value(v[s.primaryKey], s.properties[s.primaryKey])};
    }
    if (typeof v === 'number') {
      if (!Number.isFinite(v)) return {$number: String(v)};
      if (property.type === 'int' && !Number.isSafeInteger(v)) throw new Error('Unsafe integer; refusing lossy export');
      return v;
    }
    if (['string', 'boolean'].includes(typeof v)) return v;
    if (typeof v === 'bigint') return {$integer: String(v)};
    const element = {type: property.objectType};
    if (Array.isArray(v) || v instanceof Realm.List || v instanceof Realm.Set) return Array.from(v, x => value(x, element));
    if (v instanceof Realm.Dictionary || Object.getPrototypeOf(v) === Object.prototype) return Object.fromEntries(Object.entries(v).map(([k,x])=>[k,value(x, element)]));
    throw new Error('Unsupported Realm value; refusing lossy export');
  }
  const out = fs.openSync(process.argv[3], 'wx', 0o600);
  const write = x => fs.writeSync(out, JSON.stringify(x)+'\n');
  write({kind:'schema',version:1,realm_schema_version:realm.schemaVersion,schemas:[...schemas.values()]});
  for (const s of schemas.values()) {
    const rows = realm.objects(s.name);
    if (rows.length && !s.primaryKey) throw new Error('Nonempty model without primary key');
    for (const row of rows) write({kind:'object',type:s.name,id:value(row[s.primaryKey],s.properties[s.primaryKey]),data:Object.fromEntries(Object.keys(s.properties).map(k=>[k,value(row[k],s.properties[k])]))});
  }
  fs.closeSync(out);
  realm.close();
  process.exit(0);
} catch (error) {
  if (realm && !realm.isClosed) realm.close();
  console.error('Realm export failed; source unchanged, current database unchanged.');
  console.error(error.stack?.split('\n').slice(1, 4).join('\n'));
  process.exit(1);
}
