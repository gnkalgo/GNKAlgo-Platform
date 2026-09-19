import Image from "next/image";
export function Logo({compact=false}:{compact?:boolean}) {
  return <div className={compact ? "logo compact" : "logo"}><Image src="/gnkalgo-logo.jpg" alt="GnKAlgo" width={compact?150:560} height={compact?58:250} priority /></div>;
}
