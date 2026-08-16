export type InspectionStatus = "Y" | "N" | "NA";

export type ChecklistSection = {
  id: string;
  title: string;
  items: readonly { id: string; text: string }[];
};

export const CHECKLIST_SECTIONS = [
  {
    id: "general",
    title: "General",
    items: [
      { id: "general-01", text: "General site housekeeping satisfactory, free of debris/litter" },
      { id: "general-02", text: "HSE notice board / bulletin available and updated" },
      { id: "general-03", text: "Safety signage available and in good condition" },
      { id: "general-04", text: "Site access controlled; restricted areas clearly defined" },
      { id: "general-05", text: "Security gates and perimeter fencing adequate" },
      { id: "general-06", text: "Required permits (PTW/Hot Work/Excavation/Lifting) valid & displayed" },
    ],
  },
  {
    id: "welfare",
    title: "Welfare",
    items: [
      { id: "welfare-01", text: "Clean, functional toilets available (adequate ratio to workforce)" },
      { id: "welfare-02", text: "Potable drinking water available and accessible at multiple points" },
      { id: "welfare-03", text: "Handwashing facilities with soap/sanitizer provided" },
      { id: "welfare-04", text: "Rest/break shelters available, shaded and ventilated" },
      { id: "welfare-05", text: "First aid box available, fully stocked and in-date" },
      { id: "welfare-06", text: "Trained first aider available with contact details displayed" },
      { id: "welfare-07", text: "Designated smoking area away from flammable materials" },
      { id: "welfare-08", text: "Waste bins provided at welfare areas and emptied regularly" },
    ],
  },
  {
    id: "pedestrian-safety",
    title: "Pedestrian Safety",
    items: [
      { id: "pedestrian-01", text: "Pedestrian walkways segregated from vehicle/plant routes" },
      { id: "pedestrian-02", text: "Walkways free of trip hazards, debris and obstructions" },
      { id: "pedestrian-03", text: "Adequate lighting provided along pedestrian routes" },
      { id: "pedestrian-04", text: "Pedestrian crossing points clearly marked" },
      { id: "pedestrian-05", text: "Edge protection/guardrails at openings and edges along routes" },
      { id: "pedestrian-06", text: "Banksman/spotter deployed at blind spots and reversing zones" },
      { id: "pedestrian-07", text: "Site speed limit signage posted and enforced" },
    ],
  },
  {
    id: "excavation",
    title: "Excavation",
    items: [
      { id: "excavation-01", text: "Excavation permit issued, valid and displayed at location" },
      { id: "excavation-02", text: "Underground utility scan completed prior to dig" },
      { id: "excavation-03", text: "Excavation properly shored, battered or benched" },
      { id: "excavation-04", text: "Edge protection / hard barricading around full perimeter" },
      { id: "excavation-05", text: "Safe access/egress (ladder) provided within 7.5 m travel" },
      { id: "excavation-06", text: "Spoil stockpiled clear of excavation edge (min 1-1.5 m)" },
      { id: "excavation-07", text: "Daily inspection by Competent Person carried out; log maintained" },
      { id: "excavation-08", text: "Water ingress controlled / dewatering in place if required" },
    ],
  },
  {
    id: "electrical-safety",
    title: "Electrical Safety",
    items: [
      { id: "electrical-01", text: "Temporary electrical installations inspected and colour-coded/tagged" },
      { id: "electrical-02", text: "ELCB/RCD provided and trip-tested on all distribution boards" },
      { id: "electrical-03", text: "Cables/wires elevated, protected and clear of wet/muddy ground" },
      { id: "electrical-04", text: "No unauthorized joints or makeshift/temporary connections" },
      { id: "electrical-05", text: "Distribution boards labelled, lockable, and IP-rated" },
      { id: "electrical-06", text: "Earthing/bonding verified on all electrical equipment" },
      { id: "electrical-07", text: "LOTO (Lock Out Tag Out) procedure followed for maintenance work" },
      { id: "electrical-08", text: "Damaged plugs, sockets or cables replaced (not taped)" },
    ],
  },
  {
    id: "hot-work",
    title: "Hot Work",
    items: [
      { id: "hot-work-01", text: "Hot work permit issued, valid and displayed at location" },
      { id: "hot-work-02", text: "Fire watch assigned and equipped with extinguisher during/after work" },
      { id: "hot-work-03", text: "Area cleared of combustible materials within safe radius" },
      { id: "hot-work-04", text: "Correct type of fire extinguisher available at hot work location" },
      { id: "hot-work-05", text: "Gas cylinders secured upright with flashback arrestors fitted" },
      { id: "hot-work-06", text: "Welding screens/curtains used to protect nearby workers" },
      { id: "hot-work-07", text: "Correct hot work PPE worn (welding shield, gloves, apron)" },
      { id: "hot-work-08", text: "Area monitored for minimum 30 minutes after work completion" },
    ],
  },
  {
    id: "work-at-height",
    title: "Work at Height",
    items: [
      { id: "height-01", text: "Work at height permit issued where required" },
      { id: "height-02", text: "Fall protection provided (guardrails, toe boards, safety nets)" },
      { id: "height-03", text: "Full body harness with double lanyard used and correctly anchored" },
      { id: "height-04", text: "Scaffolding erected/inspected by competent person and tagged" },
      { id: "height-05", text: "Ladders in good condition, secured, and used at correct angle" },
      { id: "height-06", text: "MEWP/scissor lift operators certified and equipment inspected" },
      { id: "height-07", text: "Edge protection provided at leading edges and floor openings" },
      { id: "height-08", text: "Rescue plan in place for work at height activities" },
    ],
  },
  {
    id: "lifting-operations",
    title: "Lifting Operations",
    items: [
      { id: "lifting-01", text: "Lifting plan / method statement approved for the lift" },
      { id: "lifting-02", text: "Crane/lifting equipment has valid third-party (TPI) certificate" },
      { id: "lifting-03", text: "Crane operator holds valid, in-date competency license" },
      { id: "lifting-04", text: "Rigger / signalman certified and identifiable" },
      { id: "lifting-05", text: "Daily pre-use inspection of crane & lifting gear logged" },
      { id: "lifting-06", text: "Exclusion zone barricaded during lifting operation" },
      { id: "lifting-07", text: "Tag lines used to control suspended load" },
      { id: "lifting-08", text: "Wind speed monitored and within safe operating limits" },
    ],
  },
  {
    id: "signages",
    title: "Signages",
    items: [
      { id: "signage-01", text: "Mandatory PPE signage displayed at site entry points" },
      { id: "signage-02", text: "Hazard warning signage at excavation/lifting/electrical areas" },
      { id: "signage-03", text: "Fire assembly point & escape route signage displayed" },
      { id: "signage-04", text: "Emergency contact numbers displayed at key locations" },
      { id: "signage-05", text: "No Smoking / No Naked Flame signage near flammables" },
      { id: "signage-06", text: "Signage legible, in good condition and multilingual as needed" },
    ],
  },
  {
    id: "equipment-machinery",
    title: "Equipment & Machinery",
    items: [
      { id: "equipment-01", text: "Equipment/machinery has valid third-party (TPI) certification" },
      { id: "equipment-02", text: "Operators hold valid competency license for equipment operated" },
      { id: "equipment-03", text: "Daily pre-use inspection checklist completed for each item" },
      { id: "equipment-04", text: "Fire extinguisher provided and serviced on mobile plant" },
      { id: "equipment-05", text: "Reverse alarm / beacon light functional" },
      { id: "equipment-06", text: "Machine guards on moving/rotating parts intact" },
      { id: "equipment-07", text: "No visible oil/hydraulic leaks or structural damage" },
    ],
  },
  {
    id: "traffic-management",
    title: "Traffic Management",
    items: [
      { id: "traffic-01", text: "Traffic management plan implemented and communicated to site" },
      { id: "traffic-02", text: "Site vehicles have valid inspection / roadworthiness certificate" },
      { id: "traffic-03", text: "Drivers hold valid license appropriate to the vehicle type" },
      { id: "traffic-04", text: "Designated vehicle routes marked and segregated from pedestrians" },
      { id: "traffic-05", text: "Speed limits enforced and clearly signposted" },
      { id: "traffic-06", text: "Reversing procedures followed with banksman assistance" },
      { id: "traffic-07", text: "Vehicle movement restricted near excavation/lifting zones" },
      { id: "traffic-08", text: "Adequate lighting provided for night-time vehicle movement" },
    ],
  },
  {
    id: "man-machine-interface",
    title: "Man-Machine Interface",
    items: [
      { id: "mmi-01", text: "Segregation maintained between workers and moving plant/machinery" },
      { id: "mmi-02", text: "Exclusion zones established and enforced around operating machinery" },
      { id: "mmi-03", text: "Machine operators maintain visual/radio contact with ground personnel" },
      { id: "mmi-04", text: "Warning devices (horns, beacons) functional on all machinery" },
      { id: "mmi-05", text: "Workers wear high-visibility clothing in machine operating areas" },
      { id: "mmi-06", text: "No manual work permitted within machine swing/operating radius" },
      { id: "mmi-07", text: "Spotters/banksman deployed where blind spots exist" },
    ],
  },
  {
    id: "behaviour-based-safety",
    title: "Behaviour Based Safety",
    items: [
      { id: "bbs-01", text: "BBS observations conducted and recorded for the shift" },
      { id: "bbs-02", text: "Unsafe acts/conditions identified and corrected on the spot" },
      { id: "bbs-03", text: "Positive safety behaviour reinforced/recognized among workers" },
      { id: "bbs-04", text: "Workers actively participating in toolbox talks and briefings" },
      { id: "bbs-05", text: "Near-miss reporting encouraged and tracked" },
      { id: "bbs-06", text: "Visible safety leadership (supervisors engaging with workforce)" },
      { id: "bbs-07", text: "Corrective actions from previous BBS observations closed out" },
    ],
  },
  {
    id: "general-best-practice",
    title: "General Best Practice",
    items: [
      { id: "best-01", text: "PPE compliance verified for all workers on site" },
      { id: "best-02", text: "Housekeeping maintained across all active work areas" },
      { id: "best-03", text: "Waste segregated and disposed of per waste management plan" },
      { id: "best-04", text: "Confined space entry permit system followed where applicable" },
      { id: "best-05", text: "Environmental controls in place (dust suppression, noise control)" },
      { id: "best-06", text: "Daily toolbox talk conducted and attendance recorded" },
    ],
  },
] as const satisfies readonly ChecklistSection[];

export const CHECKLIST_ITEMS = CHECKLIST_SECTIONS.flatMap((section) =>
  section.items.map((item) => ({ ...item, sectionId: section.id, sectionTitle: section.title })),
);

export const CHECKLIST_ITEM_IDS = new Set(CHECKLIST_ITEMS.map((item) => item.id));
export const TOTAL_CHECKLIST_ITEMS = CHECKLIST_ITEMS.length;

export function calculateSummary(responses: Record<string, InspectionStatus>) {
  const values = Object.values(responses);
  const compliant = values.filter((value) => value === "Y").length;
  const nonCompliant = values.filter((value) => value === "N").length;
  const notApplicable = values.filter((value) => value === "NA").length;
  const totalInspected = compliant + nonCompliant;
  const score = totalInspected ? Math.round((compliant / totalInspected) * 1000) / 10 : 0;
  return { compliant, nonCompliant, notApplicable, totalInspected, score };
}
