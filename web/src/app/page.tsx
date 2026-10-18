import { redirect } from "next/navigation";

export default function Home() {
  // 控制台的默认入口是"今日"：一天只从这里开始，避免在菜单里迷路
  redirect("/today");
}
