// DNS override for npm under corporate DNS sinkhole.
// Usage: NODE_OPTIONS="--require <this file>" npm.cmd install
// registry.npmjs.org / registry.npmmirror.com resolve to 127.0.0.1 via
// hijacked local DNS; remap them to real IPs for this process only.
// Real IPs verified 2026-08-21 (Cloudflare anycast / npmmirror CDN).
const dns = require('dns')

const MAP = {
  'registry.npmjs.org': '104.16.0.34',
  'registry.npmmirror.com': '183.95.252.30',
}

const originalLookup = dns.lookup
function patchedLookup(domain, options, callback) {
  const mapped = MAP[domain]
  if (mapped) {
    if (typeof options === 'function') {
      callback = options
      options = {}
    }
    if (typeof callback === 'function') {
      if (options && options.all) {
        process.nextTick(() => callback(null, [{ address: mapped, family: 4 }]))
      } else {
        process.nextTick(() => callback(null, mapped, 4))
      }
      return
    }
  }
  return originalLookup.call(dns, domain, options, callback)
}

dns.lookup = patchedLookup
