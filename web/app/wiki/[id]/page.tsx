import { notFound } from "next/navigation";
import Markdown from "@/components/Markdown";
import { getPage, getWiki } from "@/lib/wiki";

export function generateStaticParams() {
  return getWiki().pages.map((p) => ({ id: p.id }));
}

export default function WikiPage({ params }: { params: { id: string } }) {
  const page = getPage(params.id);
  if (!page) notFound();
  return (
    <article className="page">
      <div className="breadcrumb">
        <a href="/">Home</a>
        <span className="sep">/</span>
        <span>{page.title}</span>
      </div>
      <Markdown>{page.markdown}</Markdown>
    </article>
  );
}
