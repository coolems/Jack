# COOLEMS COMMERCIAL LICENCE AGREEMENT (CLA) — TEMPLATE

**DRAFT v0.1 (2026-10-15) — for lawyer review before first enterprise contract.**
Governs commercial use of the COOLEMS "Jack" software by companies and organisations,
as required by section 3 of the source-available `LICENSE` in this repository.

---

**This Commercial Licence Agreement ("Agreement") is made between:**

- **Coolems KG**, a company registered in Austria, registered office: [ADDRESS],
  contact: coolems.kg@gmail.com (the "Licensor"); and
- **[COMPANY NAME]**, a legal entity registered under the laws of [JURISDICTION],
  registration no. [REG NO], with its seat at [ADDRESS] (the "Licensee").

## 1. SUBJECT MATTER & SCOPE OF THE LICENCE

1.1 The Licensor grants the Licensee a **non-exclusive, non-transferable** licence to:
   (a) install and run the COOLEMS software identified in **Annex A** (version/build,
       modules, model components) on the Licensee's own infrastructure;
   (b) use it internally for the Licensee's business activities;
   (c) receive updates and support as defined in section 6.

1.2 **Seat / user count:** this licence covers **[N] named users / seats** on up to
   **[M] server(s)**. Use beyond the licensed seat or server count requires a written
   amendment before such use begins. *(Seat count is pre-filled from the Licensee's
   registration data collected at software init — "number of users requested".)*

1.3 **Redistribution:** the Licensee may NOT redistribute, resell, sublicense, rent,
   lease or make the Software (or any part of it) available to third parties as a
   service or product without a separate written agreement. *(If the Licensee will
   offer COOLEMS-based services to its own customers, scope and pricing are defined
   in Annex A.)*

1.4 **Source code:** the Software is source-available under the repository `LICENSE`.
   The commercial rights granted here do NOT override that licence: copyright notices
   must be retained (section 4 of the repository LICENSE), derivative works remain
   subject to it, and the trademarks clause (section 6) applies unchanged.

1.5 **Third-party components:** llama.cpp, model weights, CUDA/cuBLAS and other
   third-party runtime components are governed by THEIR OWN licences (see
   `THIRD-PARTY-NOTICES.txt`). This Agreement grants no rights in them; the Licensee
   accepts those upstream terms directly.

## 2. FEES & PAYMENT

2.1 **Licence fee:** EUR [AMOUNT] per seat / per server, billed [annually / one-time].
2.2 Invoices are due within **[30] days** of invoice date. Late payments accrue interest
    at the statutory rate under Austrian law (Oesterreichischer Scheck- und Wechselgesetz).
2.3 Fees are exclusive of VAT where applicable. Fees for the current term are **non-refundable**.
2.4 The Licensor may adjust fees on renewal with **[90] days** written notice; the then-current
    fee schedule applies to renewed terms.

## 3. TERM & RENEWAL (mirrors server-side licence record)

3.1 This Agreement starts on the **issue date** and runs for the period recorded in the
    Licensor's licence register (`period` / `valid_until`), i.e. from **[ISSUE DATE]** to
    **[VALID UNTIL — or "perpetual"]**.
3.2 **Renewal:** unless either party gives written notice of non-renewal at least
    **[60] days** before the end date, this Agreement renews automatically for successive
    [12-month] terms at the fees in effect at that time.
3.3 **Expiry / suspension:** on expiry without renewal, or upon suspension by the Licensor
    (e.g. non-payment), the licence check status of the Licensee's installation changes to
    "pending" and the rights under section 1 end at the end of a **[14]-day** grace period.
    Data stored in the Software remains accessible during the grace period for export.

## 4. OBLIGATIONS OF THE LICENSEE

4.1 Use the Software only as permitted by this Agreement and applicable law.
4.2 Keep the licence credentials (licence number, registered company name / contact e-mail)
    confidential; they identify THIS installation to the Licensor's licence service.
4.3 Not remove or alter copyright notices, not bypass technical measures, and not use the
    Software for unlawful purposes.

## 5. CONFIDENTIALITY & DATA PROTECTION

5.1 Each party keeps the other's non-public business information confidential for the term
    plus [2] years.
5.2 The Licensor processes the Licensee's company name, contact e-mail and installation IP
    addresses solely to operate the licence service (verification, renewal, audit). This
    processing is based on the parties' contractual relationship (Art. 6(1)(b) GDPR); the
    Licensor acts as data controller for this purpose and will delete or anonymise the data
    upon termination of this Agreement unless retention is required by law.

## 6. SUPPORT & SLA

6.1 Support channel: coolems.kg@gmail.com (or a dedicated contact named in Annex A).
6.2 **Response targets:** critical (software unusable): [4] business hours; major feature broken:
    [1] business day; minor / question: [3] business days. *(Adjust per negotiated tier.)*
6.3 Support covers defects and usage questions, not custom development or the Licensee's own code.

## 7. INTELLECTUAL PROPERTY

All intellectual property rights in the Software remain with Coolems KG (repository LICENSE
section 4). Nothing in this Agreement transfers ownership. The names "Jack" / "Coolems KG"
and their logos stay trademarks of the Licensor; no endorsement or rebranding use is granted
(repository LICENSE section 6).

## 8. WARRANTY DISCLAIMER & LIMITATION OF LIABILITY (mirrors repository LICENSE §5)

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING
MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NON-INFRINGEMENT. IN NO EVENT SHALL THE
LICENSOR BE LIABLE FOR ANY INDIRECT, INCIDENTAL OR CONSEQUENTIAL DAMAGES. AGGREGATE LIABILITY
OF THE LICENSOR UNDER THIS AGREEMENT IS CAPPED AT **[THE FEES ACTUALLY PAID BY THE LICENSEE IN
THE [12] MONTHS PRECEDING THE CLAIM]** (or zero if nothing was paid). This cap does not apply
to liability that cannot be limited under mandatory law (e.g. gross negligence, intent, or
product-liability claims where applicable).

## 9. TERMINATION

Either party may terminate for material breach not cured within [30] days of written notice.
Sections 4.2, 5, 7 and 8 survive termination. On expiry/termination the Licensee ceases all use
per section 3.3 and deletes or returns non-public materials on request.

## 10. GOVERNING LAW & JURISDICTION (mirrors repository LICENSE §8)

This Agreement is governed by the laws of **Austria** (European Union), without regard to its
conflict-of-law rules. Any dispute shall be subject to the exclusive jurisdiction of the
competent courts at Coolems KG's registered office in Austria. *(For EU consumer-adjacent or
large enterprise counterparties, consider a negotiated alternative venue — lawyer decision.)*

## 11. GENERAL

Entire agreement; amendments only in writing; no waiver by conduct; if any clause is held
invalid the remaining clauses stay in force (severability); notices in writing to the contact
addresses above are effective on delivery.

---

### Annex A — Software & Commercial Terms (fill per contract)

| Field | Value |
|---|---|
| Licence number | COOLEMS-[XX]-[NNN] |
| Software / build | COOLEMS "Jack" v[X.Y], commit [SHA] or release tag |
| Modules included | [list — e.g. full client, relay server, tool suite] |
| Seats / servers | [N seats on M servers] |
| Model components (if delivered) | [model names + their upstream licences] |
| Issue date / valid until | [YYYY-MM-DD] / [YYYY-MM-DD or perpetual] |
| Fee & billing | [EUR …, annual / one-time] |
| Support tier | [basic / priority — see §6.2] |
| Service-offering rights (§1.3) | [none / defined scope + price] |

*Signature blocks: Coolems KG (managing partner) / Licensee (authorized signatory), date, place.*
