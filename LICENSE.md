# D-PC Messenger - Licensing

**Version:** 2.0 (Updated for D-PC Messenger)  
**Last Updated:** November 2, 2025  
**Copyright:** © 2025 Mike Shevchenko and D-PC Contributors

---

## Overview

**D-PC Messenger** is the world's first **transactional messenger** with integrated **decentralized compute sharing**. Our licensing strategy reflects our commitment to:

1. **Privacy by Design** - Code transparency ensures verifiable privacy claims
2. **User Sovereignty** - Protection from proprietary capture by tech giants
3. **Collaborative Intelligence** - Enabling secure knowledge and compute sharing
4. **Sustainable Development** - Supporting the project through optional commercial licensing

---

## License Structure

D-PC Messenger uses **Progressive Copyleft** licensing:

```
D-PC Messenger Project
│
├── Federation Hub              → AGPL v3
│   ├── Server code
│   ├── Discovery service
│   └── Resource broker
│   Why: Network service, needs strong copyleft
│
├── Messenger Client            → GPL v3
│   ├── Desktop application
│   ├── Mobile applications
│   ├── Chat interface
│   ├── AI mediator
│   ├── Context manager
│   └── Compute provider/consumer
│   Why: End-user app, privacy transparency, compute sharing
│
├── Protocol Libraries          → LGPL v3
│   ├── DPTP implementation
│   ├── Cryptographic primitives
│   ├── P2P networking
│   └── Context firewall logic
│   Why: Can be used in proprietary apps, but modifications stay open
│
├── Protocol Specifications     → CC0 (Public Domain)
│   ├── DPTP protocol spec
│   ├── .dpc_access format
│   └── Federation Hub API
│   Why: Maximum openness for the standard
│
└── Commercial License          → Available for Purchase
    For enterprises requiring proprietary modifications
```

---

## Quick Reference

### "Can I...?"

| Question | Answer | License |
|----------|--------|---------|
| **Use D-PC Messenger personally?** | ✅ YES, free | GPL v3 |
| **Use D-PC Messenger commercially?** | ✅ YES, free | GPL v3 |
| **Modify the messenger?** | ✅ YES | GPL v3 |
| **Distribute modified messenger?** | ✅ YES, must share source | GPL v3 |
| **Create proprietary messenger fork?** | ❌ NO (or buy Commercial License) | GPL v3 |
| **Use protocol libraries in my app?** | ✅ YES | LGPL v3 |
| **Keep my app proprietary?** | ✅ YES (if only using LGPL libs) | LGPL v3 |
| **Modify protocol libraries?** | ✅ YES, must share modifications | LGPL v3 |
| **Run a federation hub?** | ✅ YES, free | AGPL v3 |
| **Modify hub code?** | ✅ YES, must share if running as service | AGPL v3 |
| **Implement the protocol?** | ✅ YES, completely free | CC0 |

---

## 1. Federation Hub: GNU Affero General Public License v3 (AGPL-3.0)

### Components
- `dpc-hub/` - Federation hub server
- `dpc-discovery/` - Peer discovery service — **planned, not present in this repository**
- `dpc-resource-broker/` - Compute resource management — **planned, not present in this repository**

Components marked planned are licensed in advance so the terms are settled before the
code exists; today this licence covers `dpc-hub/`.

### Key Terms

**You MAY:**
- ✅ Use for any purpose (personal, commercial, research)
- ✅ Modify the source code
- ✅ Run as a network service
- ✅ Distribute copies

**You MUST:**
- 📋 Preserve copyright and license notices
- 📋 Provide complete source code of modifications
- 📋 License derivative works under AGPL-3.0
- 📋 **If you run modified hub as a network service, provide source to all users**

**Why AGPL?**
The AGPL's network clause prevents cloud providers from creating proprietary "D-PC Hub as a Service" without contributing back to the community.

**Full License:** See [LICENSE-AGPL](./LICENSE-AGPL) or https://www.gnu.org/licenses/agpl-3.0.html

---

## 2. Messenger Client: GNU General Public License v3 (GPL-3.0)

### Components
- `dpc-client/` - Desktop messenger application
- `dpc-mobile/` - Mobile applications (Android, iOS*) — **planned, not present in this repository**
- `dpc-cli/` - Command-line interface — **planned, not present in this repository**

The two components marked planned are licensed in advance so the terms are settled
before the code exists; today the repository ships only `dpc-client/`.

*iOS distribution subject to special handling (see App Store section below)

### Key Terms

**You MAY:**
- ✅ Use for any purpose (personal, commercial, education)
- ✅ Modify the application
- ✅ Distribute to others
- ✅ Charge for distribution services

**You MUST:**
- 📋 Preserve copyright and license notices
- 📋 Provide complete source code when distributing
- 📋 License derivative works under GPL-3.0
- 📋 Ensure recipients can modify and rebuild

**You MAY NOT:**
- ❌ Create a closed-source fork
- ❌ Remove or obscure attribution
- ❌ Combine with incompatible licenses

**Why GPL?**

1. **Privacy by Design Requires Transparency**
   - Users must be able to verify our privacy claims
   - Open source = auditable = trustworthy

2. **Protection from Big Tech Capture**
   - Prevents Apple/Google from creating proprietary forks
   - Ensures improvements benefit the community

3. **Compute Sharing Security**
   - Users can verify compute sharing is secure
   - No hidden backdoors in computation

4. **Proven Model**
   - Signal (40M+ users) is GPL
   - VLC (3B+ downloads) is GPL
   - Success depends on product quality, not permissive licensing

**Full License:** See [LICENSE-GPL](./LICENSE-GPL) or https://www.gnu.org/licenses/gpl-3.0.html

---

## 3. Protocol Libraries: GNU Lesser General Public License v3 (LGPL-3.0)

### Components
- `dpc-protocol/` - DPTP implementation, cryptographic primitives, P2P framing and
  the personal-context model — all of it lives in this one package today
- `dpc-crypto/` - Cryptographic primitives — **planned, not present in this repository**
- `dpc-p2p/` - Peer-to-peer networking — **planned, not present in this repository**
- `dpc-context/` - Context management — **planned, not present in this repository**

### Key Terms

**You MAY:**
- ✅ Use in proprietary applications
- ✅ Link dynamically or statically
- ✅ Distribute as part of proprietary software
- ✅ Charge for your proprietary application

**You MUST:**
- 📋 Provide LGPL library source if modified
- 📋 Allow users to replace the LGPL library
- 📋 Include LGPL license notice

**You MAY NOT:**
- ❌ Claim the LGPL library is proprietary
- ❌ Prevent users from accessing library modifications

**Why LGPL?**

Balance between protection and adoption:
- Libraries can be used in proprietary apps (encourages adoption)
- Modifications to libraries must be shared (protects protocol integrity)
- More enterprise-friendly than GPL

**Example Use Case:**
```python
# Your proprietary AI assistant
import dpc_protocol  # LGPL library

class MyProprietaryAI:
    def __init__(self):
        # ✅ Legal: Use LGPL library in proprietary code
        self.client = dpc_protocol.Client()
    
    def my_secret_feature(self):
        # ✅ This can remain proprietary
        pass
```

**Full License:** See [LICENSE-LGPL](./LICENSE-LGPL) or https://www.gnu.org/licenses/lgpl-3.0.html

---

## 4. Protocol Specifications: CC0 (Public Domain)

### Components
- `specs/dptp_v1.md` - DPTP protocol specification
- `specs/hub_api_v1.md` - Hub API specification

The firewall rules file (`privacy_rules.json`, formerly `.dpc_access`) has no published
spec; `dpc-client/privacy_rules.example.json` is the working reference.

### Terms

**Complete Freedom:**
- ✅ Use for any purpose
- ✅ Modify without restriction
- ✅ No attribution required (but appreciated)
- ✅ Can be used in proprietary implementations

**Why CC0?**

For D-PC to become the "SMTP for AI," the protocol specification must be:
- Completely unencumbered
- Free for anyone to implement
- No legal barriers to adoption

**Full License:** See [LICENSE-CC0](./LICENSE-CC0) or https://creativecommons.org/publicdomain/zero/1.0/

---

## 5. Commercial License (Optional)

Organizations that need to:
- Create proprietary messenger modifications
- Integrate without GPL obligations
- Remove copyleft requirements
- Get enterprise support and indemnification

Can purchase a **Commercial License**.

### Benefits

| Feature | Open Source | Commercial |
|---------|-------------|------------|
| Use messenger client | ✅ Free (GPL) | ✅ Included |
| Modify messenger | ✅ Free (must share) | ✅ Private mods OK |
| Use protocol libraries | ✅ Free (LGPL) | ✅ Included |
| Run federation hub | ✅ Free (AGPL) | ✅ Included |
| Create closed-source fork | ❌ Violates GPL | ✅ Allowed |
| Enterprise support | ❌ Community only | ✅ Dedicated SLA |
| Legal indemnification | ❌ As-is | ✅ Covered |
| Custom integrations | ❌ DIY | ✅ Professional services |

### Pricing
- **Startup:** $15,000/year (< 50 employees)
- **Growth:** $75,000/year (50-500 employees)
- **Enterprise:** Custom pricing (500+ employees)

### Contact
- Email: legoogmiha@gmail.com

---

## Compute Sharing Liability

**IMPORTANT:** When using D-PC Messenger's compute sharing features, please read
[LEGAL_DISCLAIMER.md](./LEGAL_DISCLAIMER.md) — the liability terms. A separate
compute-sharing safety guide does not exist yet; the summary below is what there is.

**Summary:**
- Platform is a tool/conduit (not liable for compute jobs)
- Users (both providers and consumers) are responsible
- Prohibited: illegal content, resource abuse, circumventing payments

---

## App Store Distribution

### Desktop (Linux, Windows, macOS)
✅ **No issues** - GPL fully compatible

### Android
✅ **No issues** - GPL allowed on Google Play and F-Droid

### iOS / Apple App Store
⚠️ **Special handling required**

**The Challenge:**
Apple App Store terms conflict with some GPL requirements (specifically, user modification rights).

**Our Solution:**
1. **Primary Distribution:** Direct download + F-Droid (no restrictions)
2. **iOS Version:** Uses GPL exception, similar to Signal and VLC
3. **Legal Precedent:** VLC and Signal both GPL on iOS

**GPL Exception for iOS:**
```
Additional permission under GNU GPL version 3 section 7:

If you convey this work as part of the Apple iOS App Store, you may
omit section 6 (Installation Information) due to Apple's restrictions.
```

**Alternative:** Progressive Web App (PWA) version bypasses app stores entirely.

---

## FAQ for Developers

### Q: Why GPL instead of Apache/MIT?

**A:** Three reasons:
1. **Privacy by Design requires transparency** - Users must verify our privacy claims
2. **Compute sharing security** - Open source ensures no hidden exploits
3. **Protection from big tech** - Prevents Apple/Google from creating closed forks

### Q: Can I use D-PC in my commercial product?

**A:** Yes!
- **If using LGPL libraries only:** ✅ Free, no restrictions
- **If modifying messenger:** ✅ Free, but share modifications (GPL)
- **If want proprietary modifications:** 💰 Commercial License

### Q: Does GPL mean I can't charge for my app?

**A:** No! GPL allows commercial use. You can:
- ✅ Sell the software
- ✅ Charge for support services
- ✅ Offer paid features
- 📋 You just must provide source code to buyers

### Q: Can Apple/Google steal D-PC code?

**A:** No.
- With GPL, they **must** open their modifications
- Or they can buy Commercial License
- Either way, project benefits

### Q: What if I just want to use the messenger?

**A:** Just download and use! GPL doesn't restrict usage, only distribution.

### Q: Do I need to open-source my AI assistant?

**A:** Only if you **distribute** a modified messenger.
- Using messenger as-is: ✅ No obligations
- Personal modifications: ✅ No obligations
- Distributing modifications: 📋 Must share source

---

## Contributing

All code contributions require signing a Contributor License Agreement (CLA).

**Why?** The CLA allows us to:
- License your contributions under multiple licenses (GPL/LGPL/AGPL + Commercial)
- Protect the project legally
- Enable sustainable business model

**You retain copyright!** See [CLA.md](./CLA.md) for details.

**Note:** Documentation contributions don't require a CLA.

---

## Trademark

"D-PC", "D-PC Messenger", and associated logos are trademarks of the D-PC Project.

**Permitted use:**
- ✅ Referring to the software
- ✅ Indicating compatibility
- ✅ Educational purposes

**Requires permission:**
- ⚠️ Commercial products using D-PC name
- ⚠️ Modified versions claiming to be "official"

Contact: legoogmiha@gmail.com

---

## License Compatibility

### Can combine with:
- ✅ GPL v3, AGPL v3, LGPL v3
- ✅ Most permissive licenses (MIT, BSD, Apache) - one way
- ✅ Other copyleft (LGPL → GPL → AGPL)

### Cannot combine with:
- ❌ GPL v2 (without compatibility clause)
- ❌ Proprietary licenses (without Commercial License)
- ❌ Licenses with additional restrictions

---

## Full License Texts

- **AGPL v3:** [LICENSE-AGPL](./LICENSE-AGPL)
- **GPL v3:** [LICENSE-GPL](./LICENSE-GPL)
- **LGPL v3:** [LICENSE-LGPL](./LICENSE-LGPL)
- **CC0:** [LICENSE-CC0](./LICENSE-CC0)

---

## Getting Help

**General Questions:**
- GitHub Discussions: https://github.com/mikhashev/dpc-messenger/discussions

**License Questions:**
- Email: legoogmiha@gmail.com

**Commercial Licensing:**
- Email: legoogmiha@gmail.com
- Phone: [To be added]

**Legal Issues:**
- Email: legoogmiha@gmail.com
- For security vulnerabilities: legoogmiha@gmail.com

---

## Legal Disclaimer & Liability

### NO WARRANTY - AS-IS BASIS

**THIS SOFTWARE IS PROVIDED "AS IS" WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE, AND NONINFRINGEMENT.**

### LIMITATION OF LIABILITY

**IN NO EVENT SHALL THE AUTHORS, COPYRIGHT HOLDERS, OR CONTRIBUTORS BE LIABLE FOR ANY CLAIM, DAMAGES, OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT, OR OTHERWISE, ARISING FROM, OUT OF, OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.**

### Important Legal Notices

1. **Educational and Research Use Only**
   - This software is provided for educational and research purposes
   - The creators accept NO LIABILITY for any consequences of use
   - Users assume ALL RISKS associated with using this software

2. **User Responsibility for Compliance**
   - YOU ARE SOLELY RESPONSIBLE for compliance with applicable laws in your jurisdiction
   - This includes but is not limited to:
     - Cryptography and encryption regulations (export controls, registration requirements)
     - Telecommunications and messaging laws
     - Data protection regulations (GDPR, CCPA, and other regional data protection laws)
     - Privacy and surveillance laws
     - Content restrictions and communication regulations

3. **Cryptographic Technology Notice**
   - This software contains strong cryptographic features (RSA-2048, TLS 1.2+, AES-256)
   - Export and use may be RESTRICTED in certain countries
   - Users must verify compliance with applicable export control laws (U.S. EAR, Wassenaar Arrangement, etc.)
   - Some jurisdictions require registration or licensing for cryptographic software

4. **Jurisdiction-Specific Considerations**
   - Users in all jurisdictions must verify compliance with local laws regarding:
     - Personal data protection and localization requirements
     - Data retention regulations
     - Cryptographic tool registration or licensing
     - Lawful surveillance and interception requirements
   - **The creators make NO GUARANTEES of compliance with any specific jurisdiction's law**
   - **Consult legal counsel before use in your jurisdiction**

5. **Prohibited Uses**
   - The creators EXPLICITLY PROHIBIT use of this software for:
     - Illegal activities under applicable law
     - Violation of export controls or sanctions
     - Circumventing lawful surveillance or interception orders
     - Activities violating third-party rights
     - Purposes contrary to public safety or national security

6. **Indemnification**
   - By using this software, you agree to INDEMNIFY AND HOLD HARMLESS the creators, contributors, and copyright holders from any claims, damages, or legal actions arising from your use

7. **Repository Access**
   - This repository may become PRIVATE without notice to limit legal exposure
   - Public access may be revoked at any time
   - Users should not redistribute without understanding their own legal obligations

8. **No Legal Advice**
   - This document provides an overview and DOES NOT constitute legal advice
   - For legal matters: consult qualified legal counsel, read complete license texts, contact us for clarification

### Compliance Recommendations

Before deploying this software, users should:
- Consult legal counsel familiar with telecommunications, data protection, and encryption laws
- Conduct a legal risk assessment for their jurisdiction
- Implement additional controls as required by local law
- Verify compliance with all applicable regulations and registration requirements in your jurisdiction

### Contact for Legal Matters

**Legal inquiries, compliance questions, or takedown requests:**
- Email: legoogmiha@gmail.com
- Response time: Best effort, no guarantees

---

## Summary

**D-PC Messenger uses GPL/LGPL/AGPL because:**

1. 🔒 **Privacy by Design** requires code transparency
2. 🛡️ **Protection** from proprietary capture
3. 🤝 **Community** improvements benefit everyone
4. 💰 **Sustainable** through optional commercial licensing
5. ✅ **Proven** model (Signal, VLC, WordPress)

**Most common use case (90% of users):**
```bash
# Download and use - that's it!
./dpc-messenger

# No license fees, no code sharing required for use
```

**For developers integrating:**
```python
# Use LGPL libraries - keep your app proprietary
from dpc_protocol import Client  # LGPL - free to use!
```

---

**Questions?** Open an issue or contact legoogmiha@gmail.com

**Last Updated:** November 2, 2025  
**Document Version:** 2.0

---

*"Building the future of collaborative intelligence, openly."*

© 2025 Mike Shevchenko and D-PC Contributors. All rights reserved.