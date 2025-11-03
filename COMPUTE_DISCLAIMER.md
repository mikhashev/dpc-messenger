# Compute Sharing Disclaimer & Terms

**D-PC Messenger - Decentralized Compute Pool**  
**Version:** 1.0  
**Effective Date:** November 2, 2025

---

## Important Notice

This document outlines the terms, limitations, and liability disclaimers specific to the **Compute Sharing** features of D-PC Messenger. By using compute sharing functionality, you acknowledge and agree to these terms.

---

## 1. What is Compute Sharing?

D-PC Messenger allows users to:
1. **Offer** their local AI inference capabilities to trusted peers
2. **Request** remote inference from other users' hardware
3. Form a **Decentralized Compute Pool** within their trusted network

Example:
```
User A (weak laptop) → Requests compute → User B (powerful PC)
                    ← Receives result ←
```

---

## 2. Platform Role & Liability

### 2.1. D-PC Messenger is a Tool, Not a Service Provider

**What we ARE:**
- ✅ A software tool that facilitates peer-to-peer connections
- ✅ A communication protocol (DPTP) for compute requests
- ✅ A conduit for encrypted data transmission

**What we are NOT:**
- ❌ A compute service provider
- ❌ A data processor for your compute jobs
- ❌ A marketplace or broker charging fees
- ❌ Responsible for compute job outcomes

### 2.2. Legal Classification

Under data protection laws (GDPR, CCPA), D-PC Messenger is a **"Tool Provider,"** not a **"Data Controller"** or **"Data Processor"** for compute transactions.

**Implication:**
- Users who share compute are Data Controllers for their own hardware
- Users who request compute are Data Controllers for their requests
- D-PC Project is NOT a party to these transactions

---

## 3. User Responsibilities

### 3.1. For Compute Providers (Those Sharing Hardware)

By offering your compute resources, you acknowledge:

**You are responsible for:**
- ✅ What hardware you choose to share
- ✅ What models you make available
- ✅ Who you grant access to (via `.dpc_access` rules)
- ✅ Monitoring your resource usage
- ✅ Complying with local laws regarding computational services

**You understand:**
- ⚠️ Compute requests are processed on your hardware
- ⚠️ You control what gets executed (via firewall rules)
- ⚠️ You can terminate sharing at any time
- ⚠️ No SLA or uptime obligations
- ⚠️ Results are provided "as-is"

**You agree NOT to:**
- ❌ Log or store compute requests from others (unless explicitly disclosed)
- ❌ Tamper with or modify computation results
- ❌ Use shared compute access to attack or exploit requesters

### 3.2. For Compute Consumers (Those Requesting Compute)

By requesting remote inference, you acknowledge:

**You are responsible for:**
- ✅ The content of your requests
- ✅ Ensuring your requests are lawful
- ✅ Respecting provider resource limits
- ✅ Not abusing the privilege

**You understand:**
- ⚠️ Compute is provided voluntarily by other users
- ⚠️ No guarantee of availability or performance
- ⚠️ Results may vary based on provider's hardware/model
- ⚠️ Provider can see request metadata (model, params) but NOT your full context
- ⚠️ Providers may terminate access without notice

**You agree NOT to:**
- ❌ Generate illegal content
- ❌ Abuse resources (spam, crypto mining, DoS attacks)
- ❌ Attempt to extract provider's proprietary data or models
- ❌ Circumvent payment systems (if provider charges)

---

## 4. Prohibited Uses

The following uses of compute sharing are **strictly prohibited:**

### 4.1. Illegal Content Generation
- ❌ Child sexual abuse material (CSAM)
- ❌ Illegal weapons instructions
- ❌ Terrorist content
- ❌ Non-consensual intimate imagery
- ❌ Content violating local laws

### 4.2. Resource Abuse
- ❌ Cryptocurrency mining without explicit permission
- ❌ Distributed denial-of-service (DDoS) attacks
- ❌ Brute-force password cracking
- ❌ Spam generation
- ❌ Any automated abuse at scale

### 4.3. Circumvention
- ❌ Bypassing provider access controls
- ❌ Reverse-engineering proprietary models
- ❌ Extracting training data from models
- ❌ Evading payment systems

### 4.4. Harmful Output
- ❌ Generating content to harass individuals
- ❌ Creating disinformation campaigns
- ❌ Producing content for fraud or scams

**Consequence:** Violation may result in:
1. Termination of compute access
2. Ban from the network
3. Legal action (in severe cases)
4. Reporting to authorities (for illegal content)

---

## 5. No Warranties

### 5.1. Compute Services Provided "AS IS"

D-PC Messenger and compute sharing functionality is provided:
- ❌ WITHOUT WARRANTY OF ANY KIND
- ❌ WITHOUT GUARANTEES of availability
- ❌ WITHOUT GUARANTEES of accuracy
- ❌ WITHOUT GUARANTEES of performance

**Explicit Disclaimers:**
```
THE COMPUTE SHARING FEATURE IS PROVIDED "AS IS" AND "AS AVAILABLE"
WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT
LIMITED TO WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR
PURPOSE, OR NON-INFRINGEMENT.

NO ADVICE OR INFORMATION, WHETHER ORAL OR WRITTEN, OBTAINED FROM
D-PC PROJECT OR THROUGH THE SERVICE SHALL CREATE ANY WARRANTY.
```

### 5.2. Third-Party Hardware

- Compute is provided by individual users, not D-PC Project
- D-PC Project has NO control over hardware quality, availability, or security
- Results depend entirely on provider's configuration

---

## 6. Limitation of Liability

### 6.1. Maximum Liability

TO THE MAXIMUM EXTENT PERMITTED BY LAW:

**D-PC Project shall NOT be liable for:**
- ❌ Any compute job results or outputs
- ❌ Availability or uptime of shared compute
- ❌ Data loss during compute transactions
- ❌ Hardware damage (provider or consumer)
- ❌ Economic losses from compute usage
- ❌ Indirect, incidental, or consequential damages
- ❌ Lost profits or business interruption

**Even if advised of the possibility of such damages.**

### 6.2. User-to-User Liability

- Liability for compute transactions lies **between users**
- If disputes arise, users must resolve directly
- D-PC Project is not a party to user disputes
- No mediation or arbitration services provided

### 6.3. Maximum Liability Cap

In jurisdictions where liability cannot be excluded, D-PC Project's maximum liability is limited to:
- **$100 USD** or
- **The amount you paid for Commercial License** (if applicable)

Whichever is greater.

---

## 7. Indemnification

By using compute sharing features, you agree to **indemnify and hold harmless** D-PC Project, its developers, and contributors from any claims arising from:

1. Your use of compute sharing (as provider or consumer)
2. Your violation of these terms
3. Your generation of illegal or harmful content
4. Your infringement of third-party rights
5. Any disputes with other users

**This includes:**
- Legal fees and costs
- Damages awarded
- Settlements

---

## 8. Privacy & Data Processing

### 8.1. What Data is Transmitted

When you request compute:
- ✅ Request metadata (model name, parameters)
- ✅ Prompt/input data for inference
- ❌ Your full Personal Context (unless explicitly shared)

When you provide compute:
- ✅ Available models list (advertised to hub)
- ✅ Availability status
- ❌ Compute request content (not logged or stored)

### 8.2. Encryption

All compute requests and responses are:
- ✅ End-to-end encrypted (P2P channel)
- ✅ Never pass through D-PC servers
- ✅ Not accessible to D-PC Project

### 8.3. Your Obligations (GDPR/CCPA)

If you process personal data through compute sharing:
- 📋 You are the Data Controller
- 📋 You must comply with applicable data protection laws
- 📋 You must obtain necessary consents
- 📋 You must respect data subject rights

---

## 9. Modifications to Terms

### 9.1. Changes

D-PC Project reserves the right to modify these terms:
- ⚠️ Changes will be announced via GitHub and project channels
- ⚠️ Major changes include 30 days notice
- ⚠️ Continued use constitutes acceptance

### 9.2. Notification

Changes will be communicated through:
- GitHub repository (CHANGELOG.md)
- In-app notifications (for major changes)

---

## 10. Termination

### 10.1. Your Right to Stop

You may stop using compute sharing at any time:
- ✅ Disable in settings
- ✅ Update `.dpc_access` rules
- ✅ No penalties for stopping

### 10.2. Network Bans

In cases of severe abuse, D-PC Project may:
- ⚠️ Add violators to a community blocklist
- ⚠️ Recommend federation hubs ban the node
- ⚠️ Report to authorities (for illegal content)

**Note:** Bans are enforced by community consensus, not centrally.

---

## 11. Jurisdiction & Dispute Resolution

### 11.1. Governing Law

These terms are governed by:
- **[Your Jurisdiction]** - To be specified

### 11.2. Disputes Between Users

For disputes with other users:
1. Attempt good-faith resolution
2. Seek mediation if needed
3. Pursue legal action if necessary

**D-PC Project is not a party to these disputes.**

### 11.3. Disputes with D-PC Project

For disputes with the project (rare):
1. Contact: legal@dpc-project.org
2. Attempt negotiation
3. Arbitration if needed
4. Litigation as last resort

---

## 12. Special Considerations

### 12.1. Enterprise Use

If using compute sharing in an enterprise context:
- ⚠️ Ensure compliance with corporate IT policies
- ⚠️ Consider security implications of shared compute
- ⚠️ May need Commercial License for indemnification

### 12.2. Educational Use

For universities/schools:
- ✅ Compute sharing can facilitate research
- ⚠️ Ensure IRB approval if processing human subjects data
- ⚠️ Comply with institutional policies

### 12.3. Healthcare

**CAUTION:** Compute sharing is NOT HIPAA compliant:
- ❌ Do not process PHI (Protected Health Information)
- ❌ Do not use for medical diagnosis
- ❌ Not approved for clinical use

---

## 13. Technical Safeguards

While D-PC Messenger includes technical safeguards, users should:

**As Provider:**
- ✅ Review `.dpc_access` rules regularly
- ✅ Monitor resource usage
- ✅ Use resource limits (max CPU/GPU, time limits)
- ✅ Keep software updated

**As Consumer:**
- ✅ Only request from trusted peers
- ✅ Verify outputs are as expected
- ✅ Don't share sensitive data without encryption
- ✅ Respect provider resources

---

## 14. Acknowledgment & Consent

By using compute sharing features of D-PC Messenger, you acknowledge:

- ✅ I have read and understood this disclaimer
- ✅ I agree to the terms and limitations
- ✅ I understand D-PC Project is not liable for compute transactions
- ✅ I am responsible for my use of compute sharing
- ✅ I will comply with all applicable laws
- ✅ I will not use compute sharing for prohibited purposes

---

## 15. Contact Information

**Questions about Compute Sharing:**
- Technical: https://github.com/mikhashev/dpc-messenger/discussions
- Legal: legoogmiha@gmail.com
- Security: legoogmiha@gmail.com

**Report Abuse:**
- Email: legoogmiha@gmail.com
- Include: node ID, timestamp, description

**Emergency/Illegal Content:**
- Email: legoogmiha@gmail.com
- Or report to local authorities

---

## 16. Additional Resources

- [Compute Sharing Safety Guide](./docs/compute-sharing-safety.md)
- [`.dpc_access` Configuration Guide](./docs/dpc-access-config.md)
- [Security Best Practices](./docs/security-best-practices.md)
- [FAQ: Compute Sharing](./docs/faq-compute-sharing.md)

---

## Summary (TL;DR)

**Compute Sharing Basics:**
- 🔧 Tool for P2P compute, not a service
- 👥 Users are responsible for their actions
- 🚫 No illegal content, abuse, or exploitation
- ⚠️ No warranties, use at your own risk
- 🛡️ D-PC Project not liable for user transactions

**Key Takeaway:**
> "With great power comes great responsibility. Use compute sharing ethically, legally, and respectfully."

---

**Document Version:** 1.0  
**Last Updated:** November 2, 2025  
**Effective Date:** November 2, 2025

© 2025 D-PC Project

---

**By clicking "I Agree" in the D-PC Messenger settings when enabling compute sharing, you accept these terms.**