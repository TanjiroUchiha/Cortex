"""Second corpus enrichment batch: remaining boilerplate docs appearing as
expected sources in the evaluation set. Frontmatter preserved byte-for-byte."""
from pathlib import Path

BODIES = {}

BODIES["facilities/hostel-room-change.md"] = """# Hostel room change request

## Summary
A hostel room change is requested on the housing portal under room change request. Changes open in week 3 of each semester, need the warden's approval, and are decided by waiting-list order and the stated reason — medical and accessibility reasons get priority.

## Details
The resident lists the current room, the requested room or block, and the reason. Approved moves happen within 7 days; the resident collects the new key and returns the old one the same day, and the access badge is reprogrammed by the hostel office. The responsible owner is the Housing Office (housing@cdu.example, ext 4620).

## Notes
A room change does not move the fee band automatically; charges are adjusted from the move date on the next statement.
"""

BODIES["academics/degree-requirements.md"] = """# Degree requirements

## Summary
A degree requires completing the programme's total credits, passing all compulsory courses, meeting the minimum CGPA, clearing attendance requirements, and finishing the mandatory internship or project where the programme includes one. The Registrar certifies degree completion only when every item is recorded.

## Details
The credit total and grade floor are published per programme on the academic portal; the degree audit report shows each requirement's status. Shortfalls in credits or attendance must be resolved before convocation registration. The responsible owner is the Registrar with the Examination Cell (registrar@cdu.example, ext 4300).

## Notes
Course substitution approvals count toward the requirement as if the substituted course were passed; unresolved substitutions delay certification.
"""

BODIES["facilities/hostel-room-maintenance.md"] = """# Hostel room maintenance

## Summary
Hostel room maintenance requests — plumbing, electrical, furniture, door locks and pest issues — are raised on the hostel maintenance portal or at the warden's office. Routine requests are attended within 48 hours; water leaks, exposed wiring and lock failures are treated as urgent and handled the same day.

## Details
The request lists the room number, issue category and a photo if possible; the resident receives a job reference and can track status on the portal. For urgent issues when the portal is down, the warden or hostel office ext 4620 is contacted directly. The responsible owner is the Facilities Maintenance team with the Housing Office.

## Notes
Damage beyond normal wear found during a repair is chargeable to the resident on the next statement.
"""

BODIES["facilities/plumbing-leak-response.md"] = """# Plumbing leak response

## Summary
A water leak in a hostel or campus building is reported as an urgent maintenance job: call the facilities emergency line ext 4600 or the hostel office, state the location and severity, and if safe, turn off the room's local isolation valve. The maintenance team isolates the supply and logs a repair job.

## Details
Leaks near electrical fittings or above ceilings are treated as emergencies — occupants move away from the area until maintenance confirms it is safe. The resident gets a job reference for follow-up and any temporary room relocation is arranged by the Housing Office. The responsible owner is Facilities Maintenance (facilities@cdu.example, ext 4600).

## Notes
Do not attempt DIY repair on building plumbing; warranty and insurance require the maintenance team to do the work.
"""

BODIES["facilities/campus-emergency-contacts.md"] = """# Campus emergency contacts

## Summary
Campus emergency contacts: Security Control Room ext 4001 (24 hours, all incidents), medical emergency ext 4111, fire and evacuation ext 4222, Facilities emergency maintenance ext 4600, and the counselling crisis line ext 4333. The control room dispatches the right responder even when the wrong line is called.

## Details
Callers state the location, nature of the emergency, injuries and hazards first; the control room may ask them to stay on the line. Non-emergency issues go through the normal service desks during office hours. The responsible owner is Campus Security (security@cdu.example).

## Notes
These numbers are posted at every building entrance and on the back of the campus ID card.
"""

BODIES["academics/attendance-condonation.md"] = """# Attendance condonation

## Summary
A student below the required attendance but within the condonable band applies for attendance condonation through the academic portal with supporting evidence — medical certificates, approved event participation or other documented absence. The condonation committee decides before exam registration closes.

## Details
The condonable band is attendance between the mandatory minimum and the condonation limit published each semester; below the band, condonation is not available and the course repeats. Approved condonation restores exam eligibility and clears the registration hold. The responsible owner is the Examination Cell with the department (exams@cdu.example, ext 4100).

## Notes
A condonation fee applies per course and is paid before the committee reviews the case.
"""

BODIES["academics/attendance-shortage.md"] = """# Attendance shortage

## Summary
Attendance shortage means a student's recorded attendance is below the minimum needed for exam eligibility. The portal shows the shortage per course; students can review sessions, file attendance corrections within 7 days of a missed punch, or apply for condonation where eligible.

## Details
Corrections go to the course instructor first; unresolved shortages are reported to the Examination Cell at the semester cutoff and block the exam registration for that course. The responsible owner is the Examination Cell with course instructors (exams@cdu.example, ext 4100).

## Notes
Attendance marked in error is corrected only through the instructor's correction form — the exam cell cannot edit attendance directly.
"""

BODIES["fees/financial-hold-release.md"] = """# Financial hold release

## Summary
A financial hold is placed for unpaid fees, library dues or other outstanding charges, and blocks exam registration, transcripts and degree documents. It is released automatically within one working day after the payment clears; urgent releases before exams are confirmed by the Fees Office on the same day.

## Details
The hold reason and amount show on the student portal's account page. Payment is made on the fees portal; a hold does not release on a pending gateway transaction — the debit must reconcile first. Disputed amounts are filed under the fee dispute process. The responsible owner is the Fees Office (fees@cdu.example, ext 4420).

## Notes
A second person's card may pay the fees but the receipt issues to the student account, not the payer.
"""

BODIES["general/campus-event-calendar.md"] = """# Campus event calendar

## Summary
The campus event calendar lists approved events, deadlines and venue bookings for the semester. Event owners submit an event request on the student services portal at least 10 working days ahead, reserve a venue, and get approval from Student Services before the event is published.

## Details
The request includes the date, venue, expected audience, equipment needs and any visitor access required. Calendar conflicts are resolved by booking order and venue rules; large events additionally need a safety review. The responsible owner is Student Services (events@cdu.example, ext 4300).

## Notes
Only approved calendar entries get visitor passes and facility setup; informal events are not supported by Facilities or Security.
"""

BODIES["facilities/contractor-check-in.md"] = """# Contractor check-in

## Summary
Contractors check in at the main gate security office on every visit: photo ID, work order or purchase order reference, and the sponsoring staff member's name are verified before a temporary contractor pass is issued. The pass is valid for the stated work window and buildings only.

## Details
The sponsor confirms the work order in advance; tools and equipment are logged at the gate and checked again on exit. Work in restricted areas needs the restricted-area access approval plus an escort. The responsible owner is Campus Security (security@cdu.example, ext 4001).

## Notes
A contractor without a verified work order is turned away; Security cannot issue access on verbal confirmation.
"""

BODIES["facilities/fire-alarm-evacuation.md"] = """# Fire alarm evacuation

## Summary
On a fire alarm, occupants evacuate by the nearest marked exit, close doors behind them, and assemble at the building's posted assembly point. Lifts are not used during an alarm. Floor wardens sweep their areas and report to the incident officer at the assembly point.

## Details
Re-entry is allowed only after Security or the fire service declares the building safe. Drills run each semester and are treated as real alarms; blocking an exit or ignoring an alarm is a conduct violation. The responsible owner is Campus Security with the fire wardens (security@cdu.example).

## Notes
Visitors and contractors follow the same procedure; the sponsoring staff member accounts for them at the assembly point.
"""

BODIES["academics/research-data-plan.md"] = """# Research data plan

## Summary
Before collecting research participant data, the project needs an approved data management plan covering what data is collected, consent handling, storage location, retention period and who may access it. The plan is filed with the ethics application and approved alongside it.

## Details
Participant consent is documented on the approved consent form; data with identifiers is stored only on the institutional encrypted store, never on personal devices or public cloud. De-identification and retention follow the schedule in the approved plan. The responsible owner is the Research Office with the Ethics Committee (research@cdu.example, ext 4700).

## Notes
A breach or lost dataset is reported to the Ethics Committee and IT Security within 24 hours.
"""

BODIES["academics/equipment-maintenance-tagout.md"] = """# Equipment maintenance tagout

## Summary
When lab equipment fails during an experiment, the researcher stops use, preserves samples under their handling protocol, and tags the instrument OUT OF SERVICE in the booking system so no one else books it. The fault is reported to the lab manager with the tagout reference.

## Details
The lab manager assigns a maintenance job; vendor service for warranty instruments is arranged through Research Facilities. The instrument returns to the booking system only after a maintenance sign-off. The responsible owner is the Lab Safety Office (labs@cdu.example, ext 4705).

## Notes
Using a tagged-out instrument is a safety violation and suspends booking rights.
"""

BODIES["academics/sample-chain-of-custody.md"] = """# Sample chain of custody

## Summary
Research samples are tracked under a chain of custody: each transfer records who collected, who received, the time, the storage condition and the sample identifier. The custody log travels with the sample and is part of the project's audit record.

## Details
Samples leaving the lab for analysis elsewhere get a custody form signed at both ends; refrigerated samples record temperature on each handover. Breaks in the chain are reported to the lab manager and noted in the project file. The responsible owner is the Lab Safety Office (labs@cdu.example, ext 4705).

## Notes
A sample without a complete custody record cannot be cited in project results or publications.
"""

BODIES["fees/procurement-thresholds.md"] = """# Procurement thresholds

## Summary
Procurement thresholds set the approval route by value: small purchases under the departmental threshold need one quote and the budget head's approval; mid-value purchases need three quotes and Procurement review; high-value purchases above the institutional threshold go through competitive tender and the Finance Committee.

## Details
Threshold amounts are published on the finance portal and reviewed annually. Splitting a purchase to stay under a threshold is a policy violation. Restricted-award spending (grants) additionally needs the Grants Office's confirmation that the item is an allowed cost. The responsible owner is the Finance Office (procurement@cdu.example, ext 4730).

## Notes
New vendors are onboarded before the first purchase order; quotes from unregistered vendors are not accepted.
"""

BODIES["fees/purchase-order-change.md"] = """# Purchase order change

## Summary
A purchase order change — quantity, price, specification or delivery date — is submitted as a change request on the procurement portal with the original PO number and the reason. The change needs the same approval level as the original order before the supplier is informed.

## Details
Price increases above the delegated limit route to the next approval tier. Changes after goods receipt are not permitted; discrepancies at the dock follow the receiving process instead. The responsible owner is Procurement and Stores (procurement@cdu.example, ext 4730).

## Notes
A supplier's verbal agreement to a change is not binding; only the amended PO on the portal counts.
"""

BODIES["fees/vendor-onboarding.md"] = """# Vendor onboarding

## Summary
A new vendor is onboarded before the first purchase order: the department submits the vendor registration form with the vendor's tax details, bank information and business registration. Finance verifies the details, runs a sanctions and duplicate check, and activates the vendor code within 5 working days.

## Details
Bank details are verified by callback to the vendor's known contact, not to the number on the request form. Vendor records are reviewed annually; dormant vendors are deactivated. The responsible owner is the Finance Office (procurement@cdu.example, ext 4730).

## Notes
Purchases from unregistered vendors are not paid; onboarding cannot be backdated to cover an order already placed.
"""

BODIES["academics/research-funding-restricted-access.md"] = """# Restricted research funding access

## Summary
Restricted awards — grants with funder-imposed conditions — can be spent only on the cost lines in the award letter. Before a department buys research equipment or services on a restricted award, the Grants Office confirms the item is an allowed cost and the funding code has balance.

## Details
The PI requests the confirmation on the research portal with the quote attached; the Grants Office replies within 5 working days. Spending outside the allowed lines is charged back to the department. The responsible owner is the Grants and Research Office (research@cdu.example, ext 4700).

## Notes
An allowed-cost confirmation covers the stated item only; a changed specification needs a fresh confirmation.
"""

BODIES["it/network-device-registration.md"] = """# Network device registration

## Summary
Every device on the campus network is registered to its owner on the IT portal before it gets full access. Registration records the device MAC address, type and owner; unregistered devices land on the limited guest network with campus-services only.

## Details
One person can register several devices; a device working on one connection but not another usually means the second device is unregistered. Registration is instant on the portal and can be checked under my devices. Problems registering go to the IT Helpdesk (it-help@cdu.example, ext 4357).

## Notes
Devices without security updates may be moved to the quarantine network until patched.
"""

BODIES["fees/library-fine-dispute.md"] = """# Library fine dispute

## Summary
A library fine dispute is filed on the library portal within 14 days with the item details and evidence — the return receipt, renewal confirmation or proof the item was returned. The library reviews the circulation log and either cancels the fine or confirms it with the reason.

## Details
Until the dispute resolves, the fine still counts toward the account total but does not block no-dues clearance if a dispute reference is on file. Lost-book charges are disputed the same way but include the replacement cost assessment. The responsible owner is the Library Services desk (library@cdu.example, ext 4330).

## Notes
A dispute filed after graduation clearance has started still pauses the no-dues block; it does not retroactively cancel a settled payment.
"""

BODIES["facilities/hostel-application.md"] = """# Hostel application and eligibility

## Summary
Students apply for hostel accommodation on the housing portal separately from academic admission: they rank available room types, note accessibility needs, and submit before the housing deadline each semester. Allocation is confirmed only by a dated Residential Life notice with the room and fee band.

## Details
Eligibility is open to enrolled full-time students; priority is first-year students, accessibility-approved students, and out-of-city applicants. After the notice, the student pays the hostel fee to hold the room and reports with photo ID at check-in. The responsible owner is the Housing Office (hostel@cdu.example, ext 4620).

## Notes
This record applies to assigned residents and approved visitors in residential facilities; day scholars apply through the day-scholar form instead.
"""

BODIES["it/it-helpdesk.md"] = """# IT Helpdesk

## Summary
The IT Helpdesk is the first contact for IT problems: account and password issues, WiFi and VPN, email, printing, device registration and software. Reach it at it-help@cdu.example, ext 4357, the portal chat, or the walk-in desk in the IT block during working hours.

## Details
Requests get a ticket reference and are worked by priority; account lockouts and outages are same-day, routine requests within 2 working days. The status page lists campus-wide outages before reporting. The responsible owner is IT Services (it-help@cdu.example, ext 4357).

## Notes
The helpdesk never asks for a password by email or phone; credential requests are always phishing and should be reported to IT Security.
"""

BODIES["facilities/access-cards.md"] = """# Campus access cards

## Summary
The campus access card opens the buildings its holder is entitled to — academic blocks, library, hostel and approved facilities. Cards are issued at enrolment or joining, activated after identity verification, and programmed for the person's current role.

## Details
Card failures are checked first at the door reader: a card that works nowhere needs re-encoding at the Facilities Service Desk; a card that fails at one door may need a permission change. Lost cards follow the lost ID card process — report, deactivate, replace. The responsible owner is the Facilities Service Desk (facilities@cdu.example, ext 4600).

## Notes
The access card is also the photo ID; it must be shown when Security or an examination invigilator asks.
"""

BODIES["facilities/hostel-access-badge.md"] = """# Hostel access badge

## Summary
The hostel access badge opens the resident's own hostel block and room floor only. It is issued at check-in with the room key, stays valid for the occupancy period, and deactivates automatically on checkout.

## Details
A badge that stops working at the hostel gate is re-encoded at the hostel office against the room record; night-time failures are handled by the duty warden or Security on ext 4001. A lost badge follows the lost ID card process — reported and deactivated before a replacement is issued. The responsible owner is the Housing Office with the warden (housing@cdu.example, ext 4620).

## Notes
The badge does not open other hostel blocks; guests are signed in at the warden's register instead.
"""

BODIES["academics/sample-chain-of-custody.md"] = BODIES["academics/sample-chain-of-custody.md"]

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
