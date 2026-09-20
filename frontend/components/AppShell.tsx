"use client";
import Link from "next/link";
import {usePathname, useRouter} from "next/navigation";
import {ChartNoAxesCombined, KeyRound, Landmark, LayoutDashboard, LogOut, ShieldCheck, Users, MonitorSmartphone, UserRound} from "lucide-react";
import {clearTokens} from "@/lib/api";
import {Logo} from "./Logo";
const links = [{href:"/dashboard",label:"Overview",icon:LayoutDashboard},{href:"/market",label:"Market data",icon:ChartNoAxesCombined},{href:"/settings/profile",label:"Profile",icon:UserRound},{href:"/settings/brokers",label:"Broker accounts",icon:Landmark},{href:"/settings/api-keys",label:"API keys",icon:KeyRound},{href:"/settings/security",label:"Security",icon:ShieldCheck},{href:"/settings/sessions",label:"Sessions",icon:MonitorSmartphone},{href:"/admin/users",label:"Admin",icon:Users}];
export function AppShell({title, kicker="CONTROL CENTRE", children}:{title:string;kicker?:string;children:React.ReactNode}) {
 const path=usePathname(), router=useRouter();
 return <main className="app-shell"><aside><Logo compact/><nav>{links.map(({href,label,icon:Icon})=><Link className={path===href?"active":""} href={href} key={href}><Icon size={18}/>{label}</Link>)}</nav><button className="nav-logout" onClick={()=>{clearTokens();router.push("/login")}}><LogOut size={18}/>Sign out</button></aside><section className="workspace"><header><div><span className="eyebrow">{kicker}</span><h1>{title}</h1></div><span className="status-pill"><i/>SYSTEM SECURE</span></header>{children}</section></main>;
}
