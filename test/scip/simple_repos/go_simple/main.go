// SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
//
// SPDX-License-Identifier: Apache-2.0

package main

import "fmt"

func main() {
	calc := NewCalculator()
	result := calc.Add(3, 5)
	fmt.Println("Result:", result)

	greeting := Greet("World")
	fmt.Println(greeting)
}
