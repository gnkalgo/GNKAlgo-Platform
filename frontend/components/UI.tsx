"use client";
export function Field({label,...props}:React.InputHTMLAttributes<HTMLInputElement>&{label:string}) {return <label className="field"><span>{label}</span><input {...props}/></label>}
export function Notice({children,tone="info"}:{children:React.ReactNode;tone?:"info"|"error"|"success"}) {return <div className={`notice ${tone}`}>{children}</div>}
export function Empty({title,copy}:{title:string;copy:string}) {return <div className="empty"><div>+</div><h3>{title}</h3><p>{copy}</p></div>}
