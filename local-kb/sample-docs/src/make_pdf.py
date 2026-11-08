"""Build kb/tidewater-handbook.pdf: three pages, one section each, so the knowledge base's
x-amz-bedrock-kb-document-page-number has something to report. Needs only ps2pdf (ghostscript).

    python make_pdf.py
"""

import subprocess
import textwrap
from pathlib import Path

PAGES = [
    (
        "Tidewater Telescope Co-op - Member Handbook",
        "1. Membership",
        "The Tidewater Telescope Co-op is a volunteer-run observatory on Gull Point. Annual "
        "membership costs 85 dollars for adults and 30 dollars for students. Members receive a key "
        "card for the dome and may book the main instrument after completing the orientation "
        "course.\n"
        "The orientation course runs on the first Saturday of every month and takes about three "
        "hours. It covers dome safety, the mount controls, and the dew heater procedure.",
    ),
    (
        None,
        "2. The main instrument",
        "The main instrument is a 16-inch Ritchey-Chretien reflector on an equatorial fork mount. "
        "Its focal ratio is f/8 and the native focal length is 3250 millimetres. The camera is a "
        "cooled monochrome sensor that must be cooled to minus 10 degrees Celsius before any "
        "science frames are taken.\n"
        "Never slew the mount while the dome shutter is closed below 40 degrees altitude; the tube "
        "can strike the shutter lip. The emergency stop is the red button on the north pier.",
    ),
    (
        None,
        "3. Booking and data",
        "Bookings are made in two-hour blocks, at most three blocks per member per week. A booking "
        "is released automatically if the member has not badged into the dome within 30 minutes of "
        "the block start.\n"
        "All raw frames are copied each morning to the co-op archive and kept for 18 months. "
        "Members who want their data kept longer must export it themselves before it expires.",
    ),
]


def ps_string(text: str) -> str:
    return "(" + text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)") + ")"


def page_ps(title: str | None, heading: str, body: str) -> list[str]:
    out, y = [], 740
    if title:
        out += ["/Helvetica-Bold findfont 16 scalefont setfont", f"72 {y} moveto {ps_string(title)} show"]
        y -= 36
    out += ["/Helvetica-Bold findfont 13 scalefont setfont", f"72 {y} moveto {ps_string(heading)} show"]
    y -= 24
    out.append("/Helvetica findfont 11 scalefont setfont")
    for paragraph in body.split("\n"):
        for line in textwrap.wrap(paragraph, 90):
            out.append(f"72 {y} moveto {ps_string(line)} show")
            y -= 15
        y -= 9
    out.append("showpage")
    return out


def main() -> None:
    here = Path(__file__).parent
    ps = ["%!PS-Adobe-3.0", "<< /PageSize [612 792] >> setpagedevice"]
    for page in PAGES:
        ps += page_ps(*page)
    ps_file = here / "tidewater-handbook.ps"
    ps_file.write_text("\n".join(ps) + "\n")
    subprocess.run(["ps2pdf", str(ps_file), str(here.parent / "kb" / "tidewater-handbook.pdf")], check=True)
    ps_file.unlink()


if __name__ == "__main__":
    main()
