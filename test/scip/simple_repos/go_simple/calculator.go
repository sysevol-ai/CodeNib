// SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
//
// SPDX-License-Identifier: Apache-2.0

package main

// Calculator performs arithmetic operations.
type Calculator struct {
	History []int
}

// NewCalculator creates a new Calculator instance.
func NewCalculator() *Calculator {
	return &Calculator{
		History: make([]int, 0),
	}
}

// Add adds two integers and records the result.
func (c *Calculator) Add(a, b int) int {
	result := a + b
	c.History = append(c.History, result)
	return result
}

// Subtract subtracts b from a and records the result.
func (c *Calculator) Subtract(a, b int) int {
	result := a - b
	c.History = append(c.History, result)
	return result
}

// LastResult returns the last recorded result.
func (c *Calculator) LastResult() int {
	if len(c.History) == 0 {
		return 0
	}
	return c.History[len(c.History)-1]
}
