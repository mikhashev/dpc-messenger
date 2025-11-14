# Geographic Restrictions and Compliance

**Document Version:** 1.0
**Last Updated:** 2025-11-14
**Status:** MANDATORY READING

---

## ⚠️ CRITICAL NOTICE

**This software is NOT intended for use in certain jurisdictions and is explicitly PROHIBITED in others.**

---

## Prohibited Jurisdictions

### ⛔ Russian Federation - EXPLICITLY PROHIBITED

**DO NOT USE THIS SOFTWARE IN THE RUSSIAN FEDERATION.**

This software is **NOT COMPLIANT** with Russian Federation law and **EXPLICITLY VIOLATES** multiple legal requirements:

#### Legal Violations

1. **Cryptography Registration (FSB/FSTEC)**
   - Violation: Uses unregistered RSA-2048, AES-256, TLS 1.2+ encryption
   - Required: FSB registration and licensing ($100,000+, 1-3 years)
   - Penalty: Fines up to 1M+ rubles, criminal prosecution
   - Status: ❌ NOT REGISTERED, NOT COMPLIANT

2. **Yarovaya Law (Federal Law 374-FZ/375-FZ)**
   - Violation: No FSB backdoor access to communications
   - Violation: No 6-month message retention
   - Violation: No 3-year metadata storage
   - Violation: Cannot comply with warrantless surveillance requests
   - Penalty: Fines up to 1M rubles per violation, service blocking, criminal charges
   - Status: ❌ EXPLICITLY NON-COMPLIANT BY DESIGN

3. **Telecommunications Operator Licensing**
   - Violation: No Roskomnadzor operator license
   - Violation: No SORM (lawful interception) infrastructure
   - Penalty: Service blocking, fines, criminal prosecution
   - Status: ❌ NOT LICENSED

4. **Personal Data Law (Federal Law 152-FZ)**
   - Violation: No data localization for Russian citizens
   - Violation: No servers physically located in Russia
   - Penalty: Fines, service blocking, criminal charges
   - Status: ❌ NOT COMPLIANT

#### Risks for Users in Russia

Using this software in Russia may result in:
- Administrative fines
- Criminal prosecution
- Service blocking
- Device seizure
- Investigation by FSB/Roskomnadzor
- Imprisonment (in severe cases)

#### Risks for Developers in Russia

Developing, distributing, or operating this software in Russia may result in:
- FSB investigation
- Criminal charges for operating illegal telecommunications services
- Charges for distributing unregistered cryptographic software
- Fines exceeding 1,000,000 rubles
- Asset seizure
- Potential imprisonment
- Travel restrictions

**DEVELOPERS IN RUSSIA: CEASE ALL DEVELOPMENT AND DISTRIBUTION IMMEDIATELY.**

---

### ⛔ Belarus - EXPLICITLY PROHIBITED

Belarus has similar restrictions to Russia regarding:
- Encryption registration requirements
- Mandatory government access to communications
- Telecommunications licensing

**DO NOT USE THIS SOFTWARE IN BELARUS.**

---

## Restricted Jurisdictions (Requires Legal Review)

### ⚠️ Jurisdictions Requiring Legal Compliance Review

Users in the following jurisdictions **MUST** consult legal counsel before use:

#### China
- Great Firewall restrictions
- Encryption approval requirements
- Multi-Level Protection Scheme (MLPS) compliance
- Data localization mandates
- Risk: Service blocking, fines, criminal charges

#### Iran
- Strict encryption controls
- Telecommunications licensing requirements
- Risk: Severe criminal penalties

#### Saudi Arabia
- CITC licensing requirements
- Encryption approval needed
- Risk: Service blocking, legal action

#### United Arab Emirates
- TRA licensing for VoIP/messaging
- Encryption restrictions
- Risk: Heavy fines, criminal prosecution

#### Turkey
- BTK licensing requirements
- Data retention obligations
- Risk: Service blocking, fines

#### Pakistan
- PTA licensing requirements
- Lawful interception mandates
- Risk: Service blocking

#### Egypt
- NTRA approval requirements
- Data retention and access obligations
- Risk: Service blocking, legal action

#### Kazakhstan
- Encryption certificate requirements
- Mandatory certificate installation for surveillance
- Risk: Criminal prosecution

#### Uzbekistan
- Government approval for encryption use
- Telecommunications licensing
- Risk: Fines, criminal charges

#### Turkmenistan
- Complete internet control by government
- All encryption likely prohibited
- Risk: Severe criminal penalties

---

## International Sanctions Compliance

### ⚠️ OFAC and International Sanctions

Users must comply with:
- U.S. Office of Foreign Assets Control (OFAC) sanctions
- EU sanctions regulations
- UK sanctions regulations
- UN Security Council sanctions

**DO NOT provide software or services to:**
- Sanctioned individuals or entities (OFAC SDN List)
- Persons in comprehensively sanctioned countries (where applicable)
- Activities that would violate sanctions

**Sanctioned countries may include** (check current lists):
- North Korea (DPRK)
- Iran (partial sanctions)
- Syria
- Cuba (partial sanctions)
- Venezuela (targeted sanctions)
- Crimea, Donetsk, Luhansk regions

**Penalties for violations:**
- Asset freezes
- Criminal prosecution
- Fines up to $20 million or twice the transaction value
- Imprisonment
- Travel bans

---

## Export Control Compliance

### Wassenaar Arrangement and Export Controls

This software contains **dual-use cryptography** subject to export controls:

**Affected jurisdictions:**
- United States: Export Administration Regulations (EAR)
- European Union: Dual-Use Regulation (EU) 2021/821
- Member countries of Wassenaar Arrangement

**Requirements:**
- May require export licenses for distribution to certain countries
- Must comply with encryption registration in some countries
- End-use and end-user restrictions apply

**Consult legal counsel regarding export compliance.**

---

## Recommended Jurisdictions (Lower Risk)

The following jurisdictions generally have favorable legal frameworks for privacy
and encryption software (but still require compliance verification):

### ✅ Generally Favorable

- **European Union members** (GDPR compliant, strong privacy protections)
  - Germany
  - Estonia
  - Netherlands
  - Switzerland (non-EU but favorable)
- **Nordic countries**
  - Iceland
  - Norway
  - Sweden
  - Finland
- **Others**
  - Canada
  - United Kingdom (post-Brexit but similar framework)
  - Australia (with some data retention obligations)
  - New Zealand
  - Japan
  - Singapore
  - United States (with export control compliance)

**NOTE:** Even in favorable jurisdictions, you must:
- Comply with data protection laws (GDPR, CCPA, etc.)
- Ensure export control compliance
- May need to implement lawful access mechanisms
- Provide required user notices and privacy policies

---

## Implementation: Geographic Blocking

### For Developers

If you deploy a Hub server, **STRONGLY RECOMMENDED** to implement IP-based geo-blocking:

#### Hub Configuration (`dpc-hub/.env`)

```bash
# Geographic restrictions (comma-separated ISO country codes)
BLOCKED_COUNTRIES=RU,BY,CN,IR,KP,SY

# Enable geo-blocking
ENABLE_GEO_BLOCKING=true

# GeoIP database path (use MaxMind GeoLite2)
GEOIP_DATABASE=/path/to/GeoLite2-Country.mmdb
```

#### Implementation Steps

1. **Install GeoIP library:**
   ```bash
   pip install geoip2
   ```

2. **Download GeoLite2 database:**
   - Register at MaxMind: https://dev.maxmind.com/geoip/geolite2-free-geolocation-data
   - Download GeoLite2-Country database

3. **Add middleware to Hub** (dpc-hub/dpc_hub/main.py):
   ```python
   import geoip2.database
   from fastapi import Request, HTTPException

   BLOCKED_COUNTRIES = ['RU', 'BY', 'CN', 'IR', 'KP', 'SY']

   @app.middleware("http")
   async def geo_blocking_middleware(request: Request, call_next):
       client_ip = request.client.host

       try:
           with geoip2.database.Reader('/path/to/GeoLite2-Country.mmdb') as reader:
               response = reader.country(client_ip)
               country_code = response.country.iso_code

               if country_code in BLOCKED_COUNTRIES:
                   raise HTTPException(
                       status_code=451,
                       detail="Service unavailable in your jurisdiction"
                   )
       except:
           # If GeoIP fails, proceed (don't block)
           pass

       return await call_next(request)
   ```

4. **Log blocked attempts:**
   - Keep logs of geo-blocking for compliance documentation
   - Demonstrates active efforts to prevent prohibited use

### For Users

**Check your jurisdiction before use:**

1. Review this document
2. Consult local legal counsel
3. Verify encryption import/export requirements
4. Ensure telecommunications licensing not required
5. Verify no conflicts with sanctions

---

## Legal Disclaimer

**This document does NOT constitute legal advice.**

The lists of prohibited, restricted, and favorable jurisdictions:
- Are NOT exhaustive
- May be outdated or incomplete
- Are subject to change without notice
- Should NOT be relied upon without independent legal verification

**YOU ARE SOLELY RESPONSIBLE** for determining whether your use complies with
applicable laws in your jurisdiction.

**CONSULT QUALIFIED LEGAL COUNSEL** before using this software in any jurisdiction.

---

## Updates and Changes

This document will be updated as legal landscapes change. However:
- Updates are best-effort only
- No guarantee of accuracy or completeness
- Users must independently verify current legal requirements

Check for updates regularly at: [Repository URL - Private Access Only]

---

## Contact

**Legal compliance questions:**
- Email: legoogmiha@gmail.com
- Subject: "LEGAL - Geographic Restrictions"

**Do NOT contact regarding:**
- Russian Federation compliance (explicitly not supported)
- Assistance circumventing restrictions
- Technical support for prohibited jurisdictions

---

## Summary

✅ **DO USE** in jurisdictions with favorable privacy/encryption laws
⚠️  **VERIFY FIRST** in jurisdictions with unclear regulations
⛔ **DO NOT USE** in Russia, Belarus, or other prohibited jurisdictions

**WHEN IN DOUBT, CONSULT LEGAL COUNSEL.**

---

**Last Updated:** 2025-11-14
**Document Version:** 1.0
**Applies to:** D-PC Messenger v0.6.1 and all versions
