// Brain wiki — components barrel re-export with brain extensions wired in.
//
// This file is a TEMPLATE. It is installed at
// `<vault>/.quartz/quartz/components/index.ts` by `brain vault render
// --overlay`, OVERWRITING the stock Quartz barrel. The overlay is a
// 1:1 file copy (`src/brain/vault/quartz_overlay.py`), so this is
// the canonical seam for ensuring the brain-extension component
// files in this directory get re-exported under the
// `Component.*` namespace consumed by `quartz.layout.ts`.
//
// Tested against Quartz v4.5.x (April 2026). If a future Quartz
// version adds a new stock component, append its export below; the
// ones that already exist are sourced verbatim from
// https://github.com/jackyzha0/quartz/blob/v4/quartz/components/index.ts
// and the brain extensions are appended at the bottom in their own
// block.
//
// brain: stock Quartz component imports + exports — keep in lock-step
// with the upstream `index.ts` linked above. If you upgrade Quartz,
// diff upstream against this block and apply additions.
import Content from "./pages/Content"
import TagContent from "./pages/TagContent"
import FolderContent from "./pages/FolderContent"
import NotFound from "./pages/404"
import ArticleTitle from "./ArticleTitle"
import Darkmode from "./Darkmode"
import ReaderMode from "./ReaderMode"
import Head from "./Head"
import PageTitle from "./PageTitle"
import ContentMeta from "./ContentMeta"
import Spacer from "./Spacer"
import TableOfContents from "./TableOfContents"
import Explorer from "./Explorer"
import TagList from "./TagList"
import Graph from "./Graph"
import Backlinks from "./Backlinks"
import Search from "./Search"
import Footer from "./Footer"
import DesktopOnly from "./DesktopOnly"
import MobileOnly from "./MobileOnly"
import RecentNotes from "./RecentNotes"
import Breadcrumbs from "./Breadcrumbs"
import Comments from "./Comments"
import Flex from "./Flex"
import ConditionalRender from "./ConditionalRender"

// brain-extension: brain-only components added by the overlay. Each
// is documented in its own file's top-of-file comment.
//   * CommandPalette — Lane C redesign — Cmd/Ctrl-K palette modal.
//     Renders hidden markup once globally (registered in
//     `quartz.layout.ts` `afterBody`); the inline script
//     `scripts/commandPalette.inline.ts` owns the open/close + fuzzy
//     search lifecycle.
import CommandPalette from "./CommandPalette"

export {
  ArticleTitle,
  Content,
  TagContent,
  FolderContent,
  Darkmode,
  ReaderMode,
  Head,
  PageTitle,
  ContentMeta,
  Spacer,
  TableOfContents,
  Explorer,
  TagList,
  Graph,
  Backlinks,
  Search,
  Footer,
  DesktopOnly,
  MobileOnly,
  RecentNotes,
  NotFound,
  Breadcrumbs,
  Comments,
  Flex,
  ConditionalRender,
  // brain-extension exports
  CommandPalette,
}
