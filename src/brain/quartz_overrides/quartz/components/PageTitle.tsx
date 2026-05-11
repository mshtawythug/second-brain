import { pathToRoot } from "../util/path"
import { classNames } from "../util/lang"
import { i18n } from "../i18n"
import { QuartzComponent, QuartzComponentConstructor, QuartzComponentProps } from "./types"

const PageTitle: QuartzComponent = ({ fileData, cfg, displayClass }: QuartzComponentProps) => {
  const title = cfg?.pageTitle ?? i18n(cfg.locale).propertyDefaults.title
  const baseDir = pathToRoot(fileData.slug!)

  return (
    <h2 class={classNames(displayClass, "page-title")}>
      <a href={baseDir}>
        <img
          class="brain-page-logo brain-page-logo-light"
          src="/static/brain-logo-light.png"
          alt=""
          aria-hidden="true"
          width="48"
          height="48"
          style="width: 3rem; height: 3rem; display: block; flex: 0 0 auto;"
        />
        <img
          class="brain-page-logo brain-page-logo-dark"
          src="/static/brain-logo-dark.png"
          alt=""
          aria-hidden="true"
          width="48"
          height="48"
          style="width: 3rem; height: 3rem; display: block; flex: 0 0 auto;"
        />
        <span>{title}</span>
      </a>
    </h2>
  )
}

PageTitle.css = `
.page-title {
  font-size: 1.75rem;
  margin: 0;
  font-family: var(--titleFont);
}

.page-title > a {
  align-items: center;
  color: inherit;
  display: inline-flex;
  gap: 0.55rem;
  text-decoration: none;
}

.brain-page-logo {
  display: block;
  flex: 0 0 auto;
  height: 3rem;
  width: 3rem;
}

.brain-page-logo-dark {
  display: none !important;
}

:root[saved-theme="dark"] .brain-page-logo-light {
  display: none !important;
}

:root[saved-theme="dark"] .brain-page-logo-dark {
  display: block !important;
}
`

export default (() => PageTitle) satisfies QuartzComponentConstructor
