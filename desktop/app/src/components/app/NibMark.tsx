import nib from "@/data/nib.svg";

export function NibMark({ size = 18 }: { size?: number }) {
  return <img src={nib} alt="" width={size} height={Math.round(size * 1.22)} className="nib-mark" />;
}
