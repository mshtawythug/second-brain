import { QuartzComponent, QuartzComponentConstructor } from "./types"

interface Options {
  links: Record<string, string>
}

export default ((opts?: Options) => {
  const githubUrl = opts?.links?.GitHub

  const Footer: QuartzComponent = () => (
    <footer>
      <p>
        Second Brain
        {githubUrl ? (
          <>
            {" "}
            (<a href={githubUrl}>GitHub</a>)
          </>
        ) : null}{" "}
        · © 2026 · notes for my future self · powered by{" "}
        <a href="https://quartz.jzhao.xyz/">Quartz</a>
      </p>
    </footer>
  )

  return Footer
}) satisfies QuartzComponentConstructor
