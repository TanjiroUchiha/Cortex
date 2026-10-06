"""One-shot corpus enrichment: replace template-boilerplate bodies in the
expanded-corpus docs with fact-bearing synthetic content keyed to the
evaluation questions. Frontmatter is preserved byte-for-byte."""
from pathlib import Path

BODIES = {}

BODIES["hr/application-steps.md"] = """# Application submission steps

## Summary
Applicants submit through the Admissions Portal in six steps: create one applicant account, choose a programme and intake, enter personal and education details, upload supporting documents, pay the application fee, and review before final submission. The portal issues a submission reference (ADMS-YYYY-NNNNN) that must be quoted in all later correspondence.

## Details
Required uploads are a government photo ID, previous institution transcripts, and a passport-size photograph; files must be PDF or JPG under 5 MB each. The application fee is paid inside the portal and a submission is not counted until payment succeeds. Applicants may save a draft and return within 30 days. The responsible owner is the Admissions Office; the service desk at admissions@cdu.example or ext 4502 answers submission problems.

## Notes
Do not create a second applicant account for the same intake; duplicates are merged and delayed. Submission after the published deadline is only accepted against a documented portal outage.
"""

BODIES["hr/application-deadlines.md"] = """# Application deadline calendar

## Summary
Each intake runs its own application deadline calendar. For the August intake, applications close on 31 March, supporting documents close on 15 April, entrance results publish on 20 May, and fee confirmation closes on 15 June. For the January intake, applications close on 31 October and documents on 15 November.

## Details
The portal timestamp controls, not email delivery: a document emailed on the deadline day but uploaded late is treated as late. Deadline reminders go to the applicant portal inbox 30, 14 and 3 days before each date. The responsible owner is the Admissions Office (admissions@cdu.example, ext 4502).

## Notes
Late submissions need a documented reason approved by the Admissions Committee; the service desk cannot approve exceptions. This calendar sets processing dates only and does not create an admission offer.
"""

BODIES["facilities/temporary-access-pass.md"] = """# Temporary campus access pass

## Summary
A temporary campus access pass lets a visitor, contractor or new joiner enter campus buildings before a permanent badge is issued. A staff sponsor requests it through the Facilities Service Desk or the security portal, and Security prints the pass at the main gate office after photo ID verification.

## Details
Passes are valid for 1 to 14 days, name the sponsor and the permitted buildings, and must be worn visibly. Contractors also need a purchase order or work order reference. The sponsor collects and returns the pass on expiry; unreturned passes are deactivated and reported to the sponsoring department. The responsible owner is Campus Security (security@cdu.example, ext 4001).

## Notes
A temporary pass never opens restricted areas such as plant rooms, labs or server rooms; those need a separate restricted-area access approval.
"""

BODIES["facilities/hostel-room-allocation.md"] = """# Hostel room allocation

## Summary
Hostel rooms are allocated through the housing portal each semester. Priority order is first-year students, students with approved accessibility needs, students from outside the city, then all remaining applicants by waiting-list order. Allocation results publish on the portal and by email.

## Details
After the allotment list, the student pays the hostel fee within 7 days to hold the room, reports to the hostel office with photo ID, signs the occupancy agreement, and collects the room key and access badge. Unpaid allotments lapse back to the waiting list. Room change requests open in week 3 of the semester and need warden approval. The responsible owner is the Housing Office (housing@cdu.example, ext 4620).

## Notes
Allocation does not include mess enrollment, which is selected separately on the housing portal.
"""

BODIES["general/official-transcript-request.md"] = """# Official transcript delivery options

## Summary
An official transcript can be delivered four ways: sealed-envelope collection at the Student Records counter, registered courier to a postal address, direct e-transcript to a named institution through the secure transcript service, or in-person collection by a nominee carrying a signed authorization letter and photo ID.

## Details
Requests are placed on the student records portal; each copy carries a fee paid online. Standard processing is 5 working days; courier adds delivery time. A transcript is official only while the envelope seal is unbroken or the e-transcript token is unopened. The responsible owner is the Registrar's Student Records Office (records@cdu.example, ext 4300).

## Notes
The office cannot email a transcript as a plain PDF attachment; that channel is not treated as official.
"""

BODIES["general/degree-certificate-application.md"] = """# Degree certificate application

## Summary
A degree certificate is applied for through the student records portal after the degree is conferred at convocation. Graduates choose a provisional certificate (issued within 10 working days for urgent proof of degree) or the final certificate (printed after the convocation list is confirmed, then collected or dispatched by registered post).

## Details
The application needs the enrolment number, programme, passing year and a fee per copy. Collection at the Student Records counter needs photo ID; postal dispatch uses the address on file. The responsible owner is the Registrar's Office (records@cdu.example, ext 4300).

## Notes
The certificate is issued only after all no-dues clearances are recorded; a pending library or fees hold blocks printing.
"""

BODIES["academics/equipment-booking.md"] = """# Shared equipment booking

## Summary
Shared research equipment is booked through the lab booking system by trained users only. A user selects the instrument, picks a slot (maximum 4 hours core hours, longer overnight by approval), and records the project or grant code the usage will be charged to.

## Details
First use of any instrument requires the supervisor's authorization plus a completed equipment induction logged by the lab manager. Bookings open 14 days ahead; two consecutive no-shows suspend booking rights for a month. Consumables and damage are charged to the booking project code. The responsible owner is the Research Facilities Office (labs@cdu.example, ext 4705).

## Notes
Equipment leaving the lab for field work needs a signed equipment movement form approved by the lab manager.
"""

BODIES["facilities/incident-reporting.md"] = """# Campus incident report intake

## Summary
Any campus incident — theft, injury, safety hazard, harassment, security breach or suspicious activity — is reported to the Security Control Room on ext 4001 (24 hours) or the online incident form on the security portal. Emergencies go to the control room first; the form follows within 24 hours.

## Details
A report states what happened, where and when, the people involved, injuries or damage, and any evidence such as photos or CCTV locations. Security issues an incident reference, logs the report in the incident register, and assigns an investigating officer. Routine reports get a first response within 4 working hours. The responsible owner is Campus Security (security@cdu.example).

## Notes
Reports may be made anonymously; anonymous reports are investigated but cannot return a personal case reference.
"""

BODIES["facilities/restricted-area-access.md"] = """# Restricted-area access request

## Summary
Restricted areas — plant rooms, server rooms, chemical stores, research labs and roof spaces — require a restricted-area access approval before entry. The requester submits the access request form naming the area, dates, purpose and a staff sponsor; Security and the area owner both approve before the badge is enabled.

## Details
Approval normally takes 3 working days and is granted for named individuals and fixed windows only. First-time access to labs and plant rooms also requires the relevant safety induction. All entries are logged by the badge system and reviewed monthly. The responsible owner is Campus Security with the area owner (security@cdu.example, ext 4001).

## Notes
A visitor enters a restricted area only escorted by an approved sponsor; a temporary pass alone never opens these doors.
"""

BODIES["general/student-record-access.md"] = """# Student record access request

## Summary
A student may inspect and obtain copies of their own student record by submitting a record access request at the Student Records counter or on the student records portal with photo ID. Access is granted within 10 working days; the first copy is free.

## Details
Third parties — including parents and employers — receive a student's record only with the student's written consent or a legal requirement. Requests to correct an inaccurate entry go to the Registrar with supporting evidence. Requests for another person's record are refused and logged. The responsible owner is the Registrar's Student Records Office (records@cdu.example, ext 4300).

## Notes
Disciplinary and counselling files are held under tighter rules and are not released through the standard counter service.
"""

BODIES["hr/identity-document-rules.md"] = """# Identity document requirements

## Summary
Employment and enrolment verification requires two identity documents: one photo document (passport, national ID card, or driving licence) and one supporting document (birth certificate, previous institution record, or utility bill showing the legal name). Documents must be originals or certified copies; photocopies alone are not accepted.

## Details
Applicants upload scans at application and present originals at first-day verification. Employees who change their legal name submit the change certificate to HR within 30 days. The responsible owner is HR Records (hr@cdu.example, ext 4410).

## Notes
An expired photo document is not acceptable; renewal receipts are accepted for up to 30 days while a replacement is issued.
"""

BODIES["fees/institutional-purchase-receiving.md"] = """# Institutional purchase receiving

## Summary
Institutional purchases are received only at the Central Stores loading dock. The delivery is checked against the purchase order and delivery note, inspected for damage, and a goods received note (GRN) is raised the same day. Payment to the supplier starts only after the GRN matches the PO and invoice.

## Details
Partial deliveries get a partial GRN and the PO stays open. Damaged or incorrect goods are refused on the dock, photographed, and logged for supplier replacement. High-value items (above the asset threshold) are asset-tagged at receipt. The responsible owner is Procurement and Stores (procurement@cdu.example, ext 4730).

## Notes
Departments may not accept deliveries at offices or labs directly; unlogged deliveries void the warranty claim process.
"""

BODIES["hr/remote-work-eligibility.md"] = """# Remote work eligibility

## Summary
An employee is eligible for remote work when the role is assessed as remote-suitable by the line manager, the employee has completed six months of service, and there is no open performance or disciplinary process. Eligible staff may work remotely up to two days a week under a remote work agreement signed by the employee and manager.

## Details
Applications go through the HR portal on the remote work form; HR and the manager decide within 10 working days. The agreement lists equipment issued, data-handling duties and core contact hours. Eligibility is reviewed every 12 months and can be withdrawn after a breach of the agreement. The responsible owner is HR Operations (hr@cdu.example, ext 4410).

## Notes
Remote access to research databases additionally requires the IT remote access review and an active VPN account.
"""

BODIES["hr/promotion-case-evidence.md"] = """# Promotion case evidence

## Summary
A promotion case needs five evidence items: the last two annual performance reviews, a manager's statement against the next-grade criteria, documented achievements (teaching, research, service or project outcomes), completed mandatory training, and an updated CV. The HR promotion committee scores the case against the published rubric.

## Details
Cases are submitted on the HR portal during the annual promotion window; incomplete cases are returned to the line manager. The committee meets twice a year and outcomes are confirmed in writing within 30 days. The responsible owner is HR Operations (hr@cdu.example, ext 4410).

## Notes
Self-nomination is allowed with a senior colleague's supporting statement when the line manager is unavailable.
"""

BODIES["hr/employee-data-privacy.md"] = """# Employee data privacy request

## Summary
An employee may submit a data privacy request to see what personal data the institution holds, to correct inaccurate data, or to ask for deletion where retention is not legally required. Requests go to the HR data privacy mailbox (privacy@cdu.example) or the HR portal and are answered within 30 days.

## Details
The requester verifies identity with an employee ID before any data is released. The response covers HR records, payroll, access logs and email retention categories. Data held under a statutory retention period — payroll, tax, disciplinary records — is disclosed but not deleted early. The responsible owner is the HR Data Protection Officer (privacy@cdu.example, ext 4418).

## Notes
Requests for another employee's data are refused unless made under a lawful authority such as a court order.
"""

BODIES["hr/workplace-accommodation.md"] = """# Workplace accommodation request

## Summary
An employee requesting a workplace accommodation for a disability or medical condition submits the accommodation request form on the HR portal with supporting medical evidence. HR's accommodation panel reviews it within 15 working days and agrees adjustments with the employee and line manager.

## Details
Typical adjustments include ergonomic equipment, modified duties, flexible hours, accessible parking and assistive software. The agreed plan is recorded on the employee's file and reviewed every 12 months or when the condition changes. The responsible owner is HR Wellbeing (hr@cdu.example, ext 4410).

## Notes
Medical details stay confidential to the panel; the line manager is told the adjustments, not the diagnosis.
"""

BODIES["facilities/lost-id-card.md"] = """# Lost ID card

## Summary
A lost campus ID card is reported immediately to the Facilities Service Desk (ext 4600) or the security portal so the card can be deactivated the same day. A replacement is issued at the service desk against photo ID and the replacement fee.

## Details
While the replacement is printed, the desk issues a temporary access pass valid up to 3 days so the cardholder can keep entering their building and hostel. Found cards are returned to the desk and deactivated cards are never reactivated — a new card number is always issued. The responsible owner is Facilities Service Desk (facilities@cdu.example, ext 4600).

## Notes
If a lost card is later found, it must still be surrendered; access logs flag any swipe attempt on a deactivated card.
"""

BODIES["facilities/security-badge-lifecycle.md"] = """# Security badge lifecycle

## Summary
A security badge moves through five stages: issue (printed after identity verification and photo), activation (enabled for the approved buildings), use (entries logged by door readers), renewal (re-validated each year or on role change), and return (surrendered and deactivated on exit). Badge access rights follow the holder's current role, not the issue date.

## Details
Lost badges are reported to Security on ext 4001 and deactivated within one hour; a replacement carries the replacement fee. Temporary visitors receive dated temporary passes instead of badges. Quarterly audits disable badges unused for 90 days. The responsible owner is Campus Security (security@cdu.example).

## Notes
A badge surrendered on exit is never reused; every badge number is permanently bound to one person.
"""

BODIES["academics/exam-registration.md"] = """# Exam registration

## Summary
Exam registration happens on the student portal during the two-week registration window each semester. The student confirms enrolled courses, pays the exam fee, and downloads the registration confirmation slip; registration is complete only when the slip is issued.

## Details
A course can only be registered if its attendance and assessment prerequisites are met; backlog exams are registered on the same form with the backlog fee. The confirmation slip lists exam codes and is needed for the hall ticket. Late registration closes 7 days before the exam period and carries a late fee. The responsible owner is the Examination Cell (exams@cdu.example, ext 4100).

## Notes
Registration errors found after the window closes are corrected only through the Examination Cell, not the department office.
"""

BODIES["academics/end-semester-examination.md"] = """# End-semester examination

## Summary
End-semester examinations run in the published exam period, with morning and afternoon sessions. Students appear only with a hall ticket generated after fee clearance and exam registration; the seating plan posts on the exam portal and notice boards 3 days before each paper.

## Details
Phones and smart watches are banned in exam halls; ID card plus hall ticket are checked at entry. Results publish on the portal within 30 days and revaluation requests open for 7 days after each result. The responsible owner is the Examination Cell (exams@cdu.example, ext 4100).

## Notes
A student more than 30 minutes late is not admitted to the hall; a missed exam follows the backlog process, not a walk-in retake.
"""

BODIES["fees/exam-fee-circular-2026.md"] = """# Exam fee circular 2026

## Summary
The 2026 exam fee circular sets the per-course exam fee, the backlog exam fee, the registration deadline and the late fee. Fees are paid only through the student portal; the registration confirmation slip is generated after payment clears.

## Details
Regular exam fee is charged per registered course and backlog exams carry a higher per-course rate. Payment after the deadline adds the late fee automatically; registrations unpaid 7 days before the exam period are cancelled. Payment failures show as pending in the portal for up to 48 hours before retry. The responsible owner is the Fees Office (fees@cdu.example, ext 4420).

## Notes
Bank debits without a portal receipt are resolved through the fee dispute process, not by re-paying immediately.
"""

BODIES["academics/external-grant-approval.md"] = """# External grant approval

## Summary
An externally funded research project needs four approvals before work starts: the principal investigator submits the proposal and budget to the Grants Office, the department head approves capacity, the Finance Office clears the budget and overhead rate, and the Ethics Committee approves any human or animal work. The Grants Office issues the award number after all four clear.

## Details
Submission is on the research portal at least 30 days before the funder deadline for internal review. The Grants Office maintains the register of funded projects and reporting dates. The responsible owner is the Grants and Research Office (research@cdu.example, ext 4700).

## Notes
Spending before the award number is issued is not reimbursable even if the funder later confirms the grant.
"""

BODIES["academics/research-ethics-review.md"] = """# Research ethics review

## Summary
Research involving human participants, animal subjects, personal data or hazardous materials requires Ethics Committee approval before the work begins. The principal investigator files the ethics application with the protocol, consent forms and risk assessment on the research portal.

## Details
Standard review returns a decision within 20 working days; expedited review covers minimal-risk studies. Approvals are valid for the stated project period and must be renewed on protocol changes. The responsible owner is the Research Ethics Committee (ethics@cdu.example, ext 4702).

## Notes
Data collected before approval cannot be added to the study afterwards; the committee treats it as a protocol violation.
"""

BODIES["academics/laboratory-access-induction.md"] = """# Laboratory access induction

## Summary
Laboratory access starts only after a three-step induction: the supervisor authorizes the person and project, the lab manager runs the safety induction covering hazards, PPE and emergency equipment, and Security activates badge access to the specific lab. The induction is logged against the person's record.

## Details
Inductions are run weekly; access activates within 2 working days of completion. Undergraduate access is always supervised during the first month. Access lapses automatically after 90 unused days and needs a refresher. The responsible owner is the Lab Safety Office (labs@cdu.example, ext 4705).

## Notes
An induction on one lab does not transfer to another; each lab has its own hazard briefing.
"""

BODIES["academics/lab-safety-rules.md"] = """# Lab safety rules

## Summary
Core lab safety rules: PPE (lab coat, eye protection, closed shoes) is mandatory in wet labs, chemicals are handled only in the designated fume hoods, eating and drinking are banned, and every incident or spill is reported to the lab manager the same day. Lone working outside office hours needs supervisor approval.

## Details
Chemical inventory is logged in the lab system; unlisted chemicals need a material approval before use. Waste goes to the labelled containers by class — sharps, solvent, biological and general. Safety equipment locations are on the lab door card. The responsible owner is the Lab Safety Office (labs@cdu.example, ext 4705).

## Notes
Repeat safety breaches suspend lab access until a re-induction is completed.
"""

BODIES["facilities/hostel-accessibility-request.md"] = """# Hostel accessibility request

## Summary
A student needing an accessible hostel room submits the hostel accessibility request on the housing portal with supporting medical or disability documentation. The Housing Office and the Accessibility Office jointly assign a suitable room — typically a ground-floor room with an accessible bathroom, or one near a lift.

## Details
Requests are processed before general allocation when filed by the housing deadline; late requests go on a priority waiting list. Rooms may include grab bars, widened doorways, visual alarms and space for a support person. The responsible owner is the Housing Office with the Accessibility Office (housing@cdu.example, ext 4620).

## Notes
An approved accessibility support plan counts as supporting documentation; the student does not resubmit medical evidence each semester.
"""

BODIES["general/accessibility-support-plan.md"] = """# Accessibility support plan

## Summary
A student with a disability or long-term condition registers with the Accessibility Office for an accessibility support plan. After an assessment meeting, the plan records approved adjustments across academics (extra exam time, accessible exam rooms, note-taking support), housing (accessible hostel rooms) and transport (accessible shuttle seats).

## Details
Registration is open year-round on the student services portal with medical or specialist documentation. The plan is shared with the Examination Cell, Housing and Transport offices so each unit applies its part; reviews happen each semester or when needs change. The responsible owner is the Accessibility Office (accessibility@cdu.example, ext 4325).

## Notes
Plan details are confidential; teaching and housing staff see the required adjustments, not the diagnosis.
"""

BODIES["it/vpn-client-troubleshooting.md"] = """# VPN client troubleshooting

## Summary
For a VPN client that will not connect, the IT checklist is: confirm the campus credentials work on the portal, complete the MFA prompt, check the device clock is within one minute of network time, then reinstall the current VPN client version from the IT portal. Most failures resolve at the MFA or version step.

## Details
On managed laptops the client updates automatically; personal devices download it from the IT portal. Connection drops inside campus networks are expected — VPN is for off-campus use and is blocked on the campus WiFi itself. Persistent failures go to the IT Helpdesk (it-help@cdu.example, ext 4357) with the client version and exact error text.

## Notes
A locked account (after repeated MFA failures) is unlocked at the Helpdesk; the VPN error message does not distinguish it from a wrong password.
"""

BODIES["it/remote-access-review.md"] = """# Remote access review

## Summary
Remote access accounts are reviewed quarterly by IT Security. Active remote work needs an approved remote work agreement, MFA enrolled, and a compliant device; accounts unused for 90 days or missing a current agreement are deactivated automatically at the review.

## Details
Managers confirm the access list each quarter; exceptions need IT Security sign-off. Deactivated access is restored through the helpdesk once the agreement and device check pass again. All remote sessions are logged and spot-checked. The responsible owner is IT Security (it-security@cdu.example, ext 4357).

## Notes
Remote access is a requirement layered on top of VPN — holding a VPN account alone does not imply remote work approval.
"""

BODIES["general/research-database-remote-access.md"] = """# Research database remote access

## Summary
Library research databases are available off campus through the library remote-access proxy using campus credentials. From the library portal, choose the database and sign in once; sessions last 8 hours. Campus VPN also works and routes traffic the same way.

## Details
Access is tied to an active library membership — expired memberships lose remote access first. Publisher licences restrict some databases to on-campus terminals only; those are marked on the portal. Problems authenticating go to the library e-resources desk (library@cdu.example, ext 4330); problems reaching the proxy go to IT Helpdesk.

## Notes
Sharing proxy credentials violates the publisher licence and suspends the account's remote access.
"""

BODIES["academics/exam-room-accommodation.md"] = """# Exam room accommodation

## Summary
An accessible examination room is arranged through the Examination Cell for students with an approved accessibility support plan or documented medical need. Adjustments include a separate or ground-floor room, extra time, a scribe or reader, assistive technology, and rest breaks.

## Details
Requests are filed on the exam accommodations form at least two weeks before the exam period so rooms and staff can be scheduled. The seating plan then lists the assigned accessible room. The responsible owner is the Examination Cell with the Accessibility Office (exams@cdu.example, ext 4100).

## Notes
A request filed inside the two-week window is handled case-by-case but cannot be guaranteed a separate room.
"""

BODIES["academics/exam-accommodation.md"] = """# Exam accommodation

## Summary
Exam accommodations are approved adjustments to exam conditions for disability, medical conditions or injury. The approved set includes extra time (typically 25%), a separate room, a scribe or reader, enlarged print papers, assistive technology and scheduled rest breaks.

## Details
Applications go through the accessibility support plan process with medical or specialist evidence; approved items apply to all exams for the plan period. Temporary injuries get a short-term approval letter from the Examination Cell. The responsible owner is the Examination Cell with the Accessibility Office (exams@cdu.example, ext 4100).

## Notes
Accommodations never change the marking standard; they change only the conditions under which the exam is taken.
"""

BODIES["it/account-deactivation.md"] = """# Account deactivation

## Summary
IT accounts are deactivated on the employee's or student's last day, based on the exit clearance submitted by HR or the Registrar. Deactivation closes email, VPN, portal and file access in one batch; the mailbox then auto-replies for 30 days.

## Details
The manager nominates a colleague to receive working files before deactivation; shared drive content transfers, personal folders do not. Email archives are retained per the retention schedule. Re-joiners get a reactivated identity, not the old mailbox contents beyond the retention window. The responsible owner is IT Account Administration (it-help@cdu.example, ext 4357).

## Notes
Urgent deactivations (security incidents, misconduct) are executed within one hour on Security's request.
"""

BODIES["fees/fee-dispute-evidence.md"] = """# Fee dispute evidence

## Summary
A fee dispute — for example an exam payment showing pending while the bank shows a debit — needs three pieces of evidence: the portal transaction reference, the bank statement line showing the debit (with date and amount), and the payment receipt or failure screenshot from the portal.

## Details
Disputes are filed on the fees portal within 14 days of the transaction. The Fees Office reconciles with the payment gateway and either posts the payment or refunds the duplicate debit within 7 working days. The responsible owner is the Fees Office (fees@cdu.example, ext 4420).

## Notes
A bank debit without a portal reference usually means a gateway timeout; the payment is not treated as made until reconciliation posts it.
"""

BODIES["fees/payment-authorization.md"] = """# Payment authorization

## Summary
Supplier payments are authorized under a two-person rule: the department certifies goods or services were received (via the GRN or service confirmation), and Finance releases payment after matching PO, GRN and invoice. No payment leaves without both signatures on the authorization record.

## Details
Invoices above delegated limits route to the next approval tier automatically. Payment runs happen weekly; urgent payments need the Finance Officer's approval note. Bank detail changes for a supplier are verified by callback to a known contact before use. The responsible owner is the Finance Office (finance@cdu.example, ext 4730).

## Notes
A bank debit at the gateway for a student-facing payment is reconciled under the fee dispute process; institutional supplier payments never use that channel.
"""

BODIES["facilities/hostel-checkout.md"] = """# Hostel checkout

## Summary
Hostel checkout has four steps before leaving: book a room inspection with the warden, return the room key and access badge, clear any outstanding hostel or mess dues at the fees counter, and collect the checkout slip. The security deposit is refunded to the registered bank account after the inspection clears.

## Details
Inspection records damage beyond normal wear; charges are deducted from the deposit and listed on the slip. Express checkout (key drop) is allowed but the deposit waits for the scheduled inspection. The checkout slip is required for the final no-dues certificate. The responsible owner is the Housing Office (housing@cdu.example, ext 4620).

## Notes
Graduating students must complete checkout before degree documents are released; the no-dues system blocks certificates until the slip is recorded.
"""

changed = 0
for rel, body in BODIES.items():
    p = Path("dataset/corpus.d") / rel
    if not p.exists():
        print("MISSING", rel)
        continue
    s = p.read_text(encoding="utf-8")
    head = s.split("---", 2)
    fm = "---" + head[1] + "---\n\n"
    p.write_text(fm + body, encoding="utf-8")
    changed += 1
print("rewrote", changed, "docs")
