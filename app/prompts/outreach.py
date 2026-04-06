OUTREACH_TEMPLATE = """
Create a Dubai-focused B2B outreach draft.

Company: {company}
Website: {website}
Pain: {pain}

Return output in a clear structure with headings:
- Ideal Customer Profile (1 line)
- Quick Audit Points (3 bullets)
- Email Subject (1)
- Email Body (max 120 words)
- WhatsApp Message (max 40 words)
- CTA (1 line)

Keep it premium and non-spammy.
"""

FOLLOWUP_TEMPLATE = """
Write follow-up messages for a Dubai-based B2B lead.

Lead:
- Company: {company}
- Website: {website}
- Contact: {contact_name}
- Email: {email}
- Phone: {phone}
- Pain: {pain}
- Status: {status}

Recent Notes / History:
{notes}

(If available) Memory snippets:
{memory}

Task:
Generate:
1) WhatsApp follow-up (Soft) - max 40 words, premium, opt-in friendly
2) WhatsApp follow-up (Direct) - max 40 words, premium, opt-in friendly
3) Email follow-up (Soft) - max 120 words
4) Email follow-up (Direct) - max 120 words
5) 1-line CTA options (2 options)
6) If they reply with: "busy", "send details", "price?" give 1 short reply for each.

Avoid spammy language. No exaggerations. Dubai professional tone.
Return with clear headings.
"""